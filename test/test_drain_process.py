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
        "LOG_LEVEL": "warning",
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


@pytest.mark.parametrize("tls,capacity", [(False, False), (False, True), (True, True)])
def test_sigterm_after_headers_finishes_body_finalizer_authority_and_keeps_probes(
    tmp_path, tls, capacity
):
    with process(tmp_path, tls=tls, capacity=capacity) as (child, client, url, probe):
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
                assert next(stream.iter_lines()) == "first"
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
            assert next(stream.iter_lines()) == "first"
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
