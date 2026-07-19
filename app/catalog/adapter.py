from __future__ import annotations

import hashlib
import json
import re
import ssl
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote, urljoin

import httpx

from app.config.settings import settings
from app.mcp.egress_policy import McpEgressPolicyError, prepare_mcp_egress_request


class CatalogAdapterError(ValueError):
    """A sanitized catalog adapter or upstream contract failure."""


@dataclass(frozen=True)
class CatalogPage:
    items: list[dict[str, Any]]
    next_cursor: str | None


@dataclass(frozen=True)
class NormalizedMcpArtifact:
    name: str
    title: str | None
    description: str
    version: str
    digest: str
    metadata: dict[str, Any]
    payload: dict[str, Any]
    compatible: bool
    incompatibility_reason: str | None
    remote_endpoints: list[dict[str, Any]]
    published_at: datetime | None
    updated_at: datetime | None


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _canonical_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


_TEMPLATE_VARIABLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _configuration_field(
    name: str,
    value: dict[str, Any],
    *,
    location: str,
    header_name: str | None = None,
) -> dict[str, Any]:
    field = {
        "name": name,
        "location": location,
        "required": bool(value.get("isRequired")),
        "secret": bool(value.get("isSecret")),
        "format": (
            value.get("format")
            if value.get("format") in {"string", "number", "boolean"}
            else "string"
        ),
    }
    if header_name:
        field["headerName"] = header_name
    for key, output_key in (
        ("description", "description"),
        ("default", "default"),
        ("placeholder", "placeholder"),
    ):
        if isinstance(value.get(key), str) and not value.get("isSecret"):
            field[output_key] = value[key]
    if isinstance(value.get("value"), str) and not value.get("isSecret"):
        field["fixedValue"] = value["value"]
    if (
        isinstance(value.get("choices"), list)
        and not value.get("isSecret")
        and all(isinstance(choice, str) for choice in value["choices"])
    ):
        field["choices"] = value["choices"]
    return field


def normalize_mcp_registry_entry(entry: dict[str, Any]) -> NormalizedMcpArtifact:
    server = entry.get("server")
    if not isinstance(server, dict):
        raise CatalogAdapterError("Registry entry is missing the server response wrapper")
    name = server.get("name")
    description = server.get("description")
    version = server.get("version")
    if not all(isinstance(value, str) and value.strip() for value in (name, description, version)):
        raise CatalogAdapterError(
            "Registry entry must include non-empty name, description, and version"
        )

    endpoints: list[dict[str, Any]] = []
    for remote in server.get("remotes") or []:
        if not isinstance(remote, dict) or remote.get("type") != "streamable-http":
            continue
        url = remote.get("url")
        if not isinstance(url, str) or not (
            url.startswith(("https://", "http://")) or _TEMPLATE_VARIABLE.match(url)
        ):
            continue
        remote_variables = (
            remote.get("variables")
            if isinstance(remote.get("variables"), dict)
            else {}
        )
        configuration_fields: list[dict[str, Any]] = []
        for variable_name, variable in remote_variables.items():
            if isinstance(variable_name, str) and isinstance(variable, dict):
                configuration_fields.append(
                    _configuration_field(variable_name, variable, location="url")
                )
        public_header_templates: list[dict[str, str]] = []
        secret_header_names: set[str] = set()
        personal_auth_header_prefix: str | None = None
        for header in remote.get("headers") or []:
            if not isinstance(header, dict) or not isinstance(header.get("name"), str):
                continue
            header_name = header["name"]
            header_variables = (
                header.get("variables")
                if isinstance(header.get("variables"), dict)
                else {}
            )
            for variable_name, variable in header_variables.items():
                if isinstance(variable_name, str) and isinstance(variable, dict):
                    configuration_fields.append(
                        _configuration_field(
                            variable_name,
                            variable,
                            location="header",
                            header_name=header_name,
                        )
                    )
            header_is_secret = bool(header.get("isSecret")) or any(
                isinstance(variable, dict) and variable.get("isSecret") is True
                for variable in header_variables.values()
            )
            header_value = header.get("value")
            if header_is_secret:
                secret_header_names.add(header_name)
                if isinstance(header_value, str):
                    matches = list(_TEMPLATE_VARIABLE.finditer(header_value))
                    if len(matches) == 1 and matches[0].end() == len(header_value):
                        personal_auth_header_prefix = header_value[: matches[0].start()]
            elif isinstance(header_value, str):
                public_header_templates.append({"name": header_name, "value": header_value})
        url_secret_variables = {
            name
            for name in _TEMPLATE_VARIABLE.findall(url)
            if isinstance(remote_variables.get(name), dict)
            and remote_variables[name].get("isSecret") is True
        }
        endpoint_supported = not url_secret_variables and len(secret_header_names) <= 1
        endpoints.append(
            {
                "type": "streamable-http",
                "url": url,
                "supported": endpoint_supported,
                "requiresConfiguration": bool(configuration_fields),
                "requiresPersonalAuth": bool(secret_header_names),
                "headerNames": sorted(
                    {
                        header.get("name")
                        for header in remote.get("headers") or []
                        if isinstance(header, dict)
                        and isinstance(header.get("name"), str)
                    }
                ),
                "secretHeaderNames": sorted(secret_header_names),
                "personalAuthHeaderPrefix": personal_auth_header_prefix,
                "configurationFields": configuration_fields,
                "publicHeaderTemplates": public_header_templates,
            }
        )
    compatible = any(endpoint.get("supported") for endpoint in endpoints)
    registry_meta = entry.get("_meta") if isinstance(entry.get("_meta"), dict) else {}
    official_meta = registry_meta.get("io.modelcontextprotocol.registry/official")
    if not isinstance(official_meta, dict):
        official_meta = {}
    return NormalizedMcpArtifact(
        name=name.strip(),
        title=server.get("title") if isinstance(server.get("title"), str) else None,
        description=description.strip(),
        version=version.strip(),
        digest=_canonical_digest(server),
        metadata={
            "registry": official_meta,
            "repository": server.get("repository"),
            "websiteUrl": server.get("websiteUrl"),
            "icons": server.get("icons") or [],
        },
        payload=server,
        compatible=compatible,
        incompatibility_reason=(
            None
            if compatible
            else "No installable remote Streamable HTTP endpoint is published."
        ),
        remote_endpoints=endpoints,
        published_at=_parse_datetime(official_meta.get("publishedAt")),
        updated_at=_parse_datetime(official_meta.get("updatedAt")),
    )


class McpRegistryV01Adapter:
    """Read-only adapter for the official MCP Registry v0.1 wire contract."""

    def __init__(
        self,
        base_url: str,
        *,
        base_path: str = "/v0.1",
        headers: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/") + "/"
        self.base_path = "/" + base_path.strip("/")
        self.headers = dict(headers or {})
        self._client = client

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, f"{self.base_path.strip('/')}/{path.lstrip('/')}")

    @staticmethod
    def _ssl_context() -> ssl.SSLContext:
        context = httpx.create_ssl_context()
        if settings.MCP_EGRESS_CA_BUNDLE_FILE.strip():
            try:
                context.load_verify_locations(cafile=settings.MCP_EGRESS_CA_BUNDLE_FILE.strip())
            except OSError as exc:
                raise CatalogAdapterError("Catalog CA bundle could not be loaded") from exc
        return context

    async def _get(
        self, path: str, *, params: dict[str, str | int] | None = None
    ) -> dict[str, Any]:
        url = self._url(path)
        try:
            target = await prepare_mcp_egress_request(url)
        except McpEgressPolicyError as exc:
            raise CatalogAdapterError(str(exc)) from exc
        request_headers = {**self.headers, "Host": target.host_header}
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.CATALOG_REQUEST_TIMEOUT_MS / 1000),
            verify=self._ssl_context(),
            follow_redirects=False,
        )
        try:
            response = await client.get(
                target.connection_url,
                params=params,
                headers=request_headers,
                extensions=target.extensions,
            )
            response.raise_for_status()
            if len(response.content) > settings.CATALOG_MAX_RESPONSE_BYTES:
                raise CatalogAdapterError("Registry response exceeds the configured size limit")
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            raise CatalogAdapterError("Registry request failed") from exc
        finally:
            if owns_client:
                await client.aclose()
        if not isinstance(payload, dict):
            raise CatalogAdapterError("Registry response must be a JSON object")
        return payload

    @staticmethod
    def _page(payload: dict[str, Any]) -> CatalogPage:
        servers = payload.get("servers")
        if not isinstance(servers, list) or not all(isinstance(item, dict) for item in servers):
            raise CatalogAdapterError("Registry list response is missing servers[]")
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        cursor = metadata.get("nextCursor")
        return CatalogPage(servers, cursor if isinstance(cursor, str) and cursor else None)

    async def probe(self) -> None:
        self._page(await self._get("servers", params={"limit": 1}))

    async def list_updated(
        self, *, cursor: str | None = None, updated_since: datetime | None = None
    ) -> CatalogPage:
        params: dict[str, str | int] = {"limit": settings.CATALOG_SYNC_PAGE_SIZE}
        if cursor:
            params["cursor"] = cursor
        if updated_since:
            params["updated_since"] = updated_since.isoformat().replace("+00:00", "Z")
        return self._page(await self._get("servers", params=params))

    async def list_versions(self, server_name: str) -> CatalogPage:
        return self._page(
            await self._get(f"servers/{quote(server_name, safe='')}/versions")
        )

    async def resolve(self, server_name: str, version: str) -> dict[str, Any]:
        payload = await self._get(
            f"servers/{quote(server_name, safe='')}/versions/{quote(version, safe='')}"
        )
        if not isinstance(payload.get("server"), dict):
            raise CatalogAdapterError("Registry version response is missing server")
        return payload

    async def fetch_artifact(self, server_name: str, version: str) -> NormalizedMcpArtifact:
        return normalize_mcp_registry_entry(await self.resolve(server_name, version))
