"""Native SIGTERM coverage pins Uvicorn capture_signals and complete ASGI cleanup."""

import os
import signal
import socket
import ssl
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def wait_for(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for native process fixture")


def certificates(root):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = root / "cert.pem", root / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    context = ssl.create_default_context(cafile=str(cert_path))
    context.load_cert_chain(cert_path, key_path)
    return context, cert_path, key_path


@contextmanager
def process(root, *, tls=False, capacity=True, mode="stream", timeout=3, cleanup=2):
    business, probe = port(), port()
    env = {
        **os.environ,
        "PYTHONPATH": str(Path.cwd()),
        "DRAIN_TEST_ROOT": str(root),
        "DRAIN_TEST_MODE": mode,
        "DRAIN_TEST_CLEANUP": str(cleanup),
        "GATEWAY_HTTP_ADDR": "127.0.0.1",
        "GATEWAY_PORT": str(business),
        "SERVICE_DRAIN_TIMEOUT_SECONDS": str(timeout),
        "WORKSPACE_CAPACITY_ENABLED": str(capacity).lower(),
        "INTERNAL_TRANSPORT_TLS_ENABLED": str(tls).lower(),
        "LOG_LEVEL": "info",
    }
    context = True
    if tls:
        context, cert, key = certificates(root)
        env.update(
            INTERNAL_TRANSPORT_HEALTH_PORT=str(probe),
            INTERNAL_TRANSPORT_TLS_CA_FILE=str(cert),
            INTERNAL_TRANSPORT_TLS_CERT_FILE=str(cert),
            INTERNAL_TRANSPORT_TLS_KEY_FILE=str(key),
            INTERNAL_TRANSPORT_TLS_REQUIRE_CLIENT_CERT="true",
            AUTH_JWKS_URL="https://localhost/jwks.json",
            BUILTIN_TARGET_MCP_SERVER_URL="https://localhost/internal/v1/mcp",
            ORCH_BASE_URL="https://localhost",
        )
    url = f"{'https://localhost' if tls else 'http://127.0.0.1'}:{business}"
    probe_url = f"http://127.0.0.1:{probe}" if tls else url
    with (root / "process.log").open("w") as log:
        child = subprocess.Popen(
            [sys.executable, "test/support/drain_process.py"], env=env, stdout=log, stderr=log
        )
        try:
            with httpx.Client(verify=context, timeout=5, trust_env=False) as client:

                def started():
                    if child.poll() is not None:
                        raise AssertionError((root / "process.log").read_text())
                    try:
                        return client.get(f"{probe_url}/ready").status_code == 200
                    except httpx.HTTPError:
                        return False

                wait_for(started)
                yield child, client, url, probe_url
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize(
    "tls,capacity", [(False, False), (False, True), (True, False), (True, True)]
)
def test_sigterm_after_headers_finishes_body_finalizer_authority_and_keeps_probes(
    tmp_path, tls, capacity
):
    with process(tmp_path, tls=tls, capacity=capacity) as (child, client, url, probe):
        expected_health = {
            "status": "ok",
            "capacity_contract_version": 1,
            "capacity_enabled": capacity,
        }
        for endpoint in (f"{url}/health", f"{url}/api/v1/health", f"{probe}/health"):
            response = client.get(endpoint)
            assert response.status_code == 200
            assert response.json() == expected_health
        if tls:
            with (
                httpx.Client(
                    verify=ssl.create_default_context(cafile=str(tmp_path / "cert.pem")),
                    trust_env=False,
                ) as anonymous,
                pytest.raises(httpx.HTTPError),
            ):
                anonymous.get(f"{url}/health")
        with client.stream("GET", f"{url}/fixture/stream") as stream:
            lines = stream.iter_lines()
            assert next(lines) == "first"
            wait_for(lambda: (tmp_path / "headers_sent").exists())
            child.send_signal(signal.SIGTERM)
            wait_for(lambda: client.get(f"{probe}/ready").status_code == 503)
            assert client.get(f"{probe}/health").status_code == 200
            rejection = client.post(f"{url}/fixture/write", json={"approval_receipt": "unused"})
            assert rejection.status_code == 503
            assert rejection.json()["error"]["outcome"] == "not_started"
            assert rejection.headers["connection"] == "close"
            assert not (tmp_path / "receipt_claimed").exists()
            child.send_signal(signal.SIGTERM)
            (tmp_path / "release_body").touch()
            assert next(lines) == "last"
            wait_for(lambda: (tmp_path / "finalizing").exists())
            assert child.poll() is None
            assert not (tmp_path / "lifespan_cleanup").exists()
            (tmp_path / "release_cleanup").touch()
            assert list(lines) == []
        assert child.wait(timeout=5) == 0, (tmp_path / "process.log").read_text()
        assert (tmp_path / "authority_finished").exists() is capacity
        assert (tmp_path / "lifespan_cleanup").exists()


@pytest.mark.parametrize("mode", ["deadline", "resist_cancel", "stuck_lifespan"])
def test_shutdown_deadline_bounds_noncooperative_work(tmp_path, mode):
    with process(tmp_path, mode=mode, timeout=0.25, cleanup=1) as (child, client, url, probe):
        if mode == "stuck_lifespan":
            child.send_signal(signal.SIGTERM)
            assert child.wait(timeout=4) == 1
        else:
            with client.stream("GET", f"{url}/fixture/stream") as stream:
                lines = stream.iter_lines()
                assert next(lines) == "first"
                started = time.monotonic()
                child.send_signal(signal.SIGTERM)
                time.sleep(0.15)
                child.send_signal(signal.SIGTERM)
                exit_code = child.wait(timeout=4)
                assert time.monotonic() - started < 3
                assert exit_code == (1 if mode == "resist_cancel" else 0), (
                    tmp_path / "process.log"
                ).read_text()
                if mode == "deadline":
                    assert (tmp_path / "cancelled").exists()
                    assert (tmp_path / "authority_finished").exists()


@pytest.mark.parametrize("disconnect", [False, True])
def test_signal_during_awaited_authorization_keeps_admitted_handler_tracked(tmp_path, disconnect):
    with process(tmp_path) as (child, client, url, probe):
        address = httpx.URL(url)
        with socket.create_connection((address.host, address.port)) as connection:
            connection.sendall(b"GET /fixture/authorization HTTP/1.1\r\nHost: localhost\r\n\r\n")
            wait_for(lambda: (tmp_path / "authorization_waiting").exists())
            if disconnect:
                connection.close()
            child.send_signal(signal.SIGTERM)
            wait_for(lambda: client.get(f"{probe}/ready").status_code == 503)
            assert child.poll() is None
            assert not (tmp_path / "lifespan_cleanup").exists()
            (tmp_path / "release_authorization").touch()
            if not disconnect:
                connection.settimeout(5)
                assert b"200 OK" in connection.recv(4096)
        assert child.wait(timeout=5) == 0
        assert (tmp_path / "authorization_completed").exists()
        assert (tmp_path / "lifespan_cleanup").exists()


def test_real_essential_worker_failure_still_exits_nonzero_during_drain(tmp_path):
    with process(tmp_path, mode="worker_failure", timeout=10) as (child, client, url, probe):
        with client.stream("GET", f"{url}/fixture/stream") as stream:
            lines = stream.iter_lines()
            assert next(lines) == "first"
            child.send_signal(signal.SIGTERM)
            wait_for(lambda: client.get(f"{probe}/ready").status_code == 503)
            (tmp_path / "fail_worker").touch()
            # Release the accepted request only after the essential worker has ended.
            wait_for(lambda: client.get(f"{probe}/health").status_code == 503)
            # Keep work active: failure must exit before the ten-second drain window.
            (tmp_path / "release_cleanup").touch()
            assert child.wait(timeout=5) == 1
        assert (
            "Essential service task stopped unexpectedly" in (tmp_path / "process.log").read_text()
        )


def test_drain_deadline_reserves_cleanup_time_for_authority_and_lifespan(tmp_path):
    with (
        process(tmp_path, mode="deadline_cleanup", timeout=0.25, cleanup=3) as (
            child,
            client,
            url,
            probe,
        ),
        client.stream("GET", f"{url}/fixture/stream") as stream,
    ):
        lines = stream.iter_lines()
        assert next(lines) == "first"
        signalled_at = time.monotonic()
        child.send_signal(signal.SIGTERM)
        wait_for(lambda: (tmp_path / "cancelled").exists(), timeout=4)
        cancelled_at = float((tmp_path / "cancelled").read_text())
        assert cancelled_at - signalled_at < 1, "Cancel at drain deadline, not near reserve expiry"
        assert child.wait(timeout=4) == 0, (tmp_path / "process.log").read_text()
        assert (tmp_path / "authority_finished").exists()
        assert (tmp_path / "lifespan_finished").exists()
        assert (tmp_path / "authority_finished").stat().st_mtime_ns <= (
            tmp_path / "lifespan_cleanup"
        ).stat().st_mtime_ns
        assert time.monotonic() - signalled_at < 3.5


WATCHDOG_PROCESS = """
import asyncio, importlib, os, runpy, threading, time
from pathlib import Path
import uvicorn
from app.service_server import ServiceServers

original_init = ServiceServers.__init__
def short_reserve(self, configs, **kwargs):
    kwargs.update(healthy=lambda: True, shutdown_timeout=0.3)
    original_init(self, configs, **kwargs)
ServiceServers.__init__ = short_reserve
root = Path(os.environ['WATCHDOG_TEST_ROOT'])
mode = os.environ['WATCHDOG_TEST_MODE']

class Listener:
    def __init__(self, config):
        self.config, self.started, self.should_exit = config, False, False
    async def serve(self):
        if mode == 'lingering_thread':
            entered = threading.Event()
            def dependency_call():
                entered.set()
                time.sleep(1.5)
            asyncio.create_task(asyncio.to_thread(dependency_call))
            while not entered.is_set():
                await asyncio.sleep(0.001)
        self.started = True
        (root / 'listener_ready').touch()
        while not self.should_exit:
            await asyncio.sleep(0.001)
        (root / 'listener_closed').touch()

uvicorn.Server = Listener
if mode == 'embedded':
    launcher = importlib.import_module('app.serve')
    asyncio.run(launcher.main())
    time.sleep(0.6)
    (root / 'embedding_survived').touch()
else:
    runpy.run_module('app.serve', run_name='__main__')
"""


@pytest.mark.parametrize("mode", ["lingering_thread", "healthy", "embedded"])
def test_native_watchdog_covers_executor_teardown_without_delaying_clean_exit(tmp_path, mode):
    env = {
        **os.environ,
        "PYTHONPATH": str(Path.cwd()),
        "INTERNAL_TRANSPORT_TLS_ENABLED": "false",
        "WORKSPACE_CAPACITY_ENABLED": "false",
        "SERVICE_DRAIN_TIMEOUT_SECONDS": "0.2",
        "WATCHDOG_TEST_ROOT": str(tmp_path),
        "WATCHDOG_TEST_MODE": mode,
    }
    with (tmp_path / "watchdog.log").open("w") as log:
        child = subprocess.Popen(
            [sys.executable, "-c", WATCHDOG_PROCESS], env=env, stdout=log, stderr=log
        )
        try:
            wait_for(lambda: (tmp_path / "listener_ready").exists())
            started = time.monotonic()
            child.send_signal(signal.SIGTERM)
            exit_code = child.wait(timeout=3)
            elapsed = time.monotonic() - started
            assert (tmp_path / "listener_closed").exists()
            assert exit_code == (1 if mode == "lingering_thread" else 0), (
                tmp_path / "watchdog.log"
            ).read_text()
            if mode == "embedded":
                assert (tmp_path / "embedding_survived").exists()
            else:
                assert elapsed < 0.9, "Process exceeded the first-signal drain and cleanup bound"
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)
