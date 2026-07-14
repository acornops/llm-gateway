<p align="center">
  <img width="220" src="https://raw.githubusercontent.com/acornops/docs-website/main/logo/light.svg" alt="AcornOps" />
</p>

<h1 align="center">AcornOps LLM Gateway</h1>

<p align="center">
  <a href="https://github.com/acornops/llm-gateway/actions/workflows/ci.yaml"><img src="https://github.com/acornops/llm-gateway/actions/workflows/ci.yaml/badge.svg" alt="CI" /></a>
  <a href="https://codecov.io/gh/acornops/llm-gateway"><img src="https://codecov.io/gh/acornops/llm-gateway/branch/main/graph/badge.svg" alt="Coverage" /></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12-blue.svg" alt="Python 3.12" /></a>
  <a href="docs/contracts/README.md"><img src="https://img.shields.io/badge/contracts-checked-blue.svg" alt="Contracts checked" /></a>
</p>

<p align="center">
  Production-grade LLM gateway for inference proxying and MCP tool brokering.
</p>

## Status

This repository owns the gateway service code, production image, health/readiness contract, metrics, and service-level docs. Full-system deployment wiring belongs in `acornops-deployment`.

## Agent-Assisted Development

This repository supports human and agent-assisted development. Start coding agents from this repository root for llm-gateway-only work, and from the `acornops-workspace` root for changes that touch multiple AcornOps repositories.

## Contracts

Cross-repo contract documentation lives in [`docs/contracts/README.md`](docs/contracts/README.md). Treat that file as the source of truth for control-plane, execution-engine, and MCP integration boundaries.
Machine-readable contract data lives in [`docs/contracts/manifest.json`](docs/contracts/manifest.json).
Run `task contracts:check` to mechanically verify the documented contracts against the implementation.

Coverage is generated in CI with `pytest-cov`, uploaded as a workflow artifact, and published to Codecov when `CODECOV_TOKEN` is configured for the repository.

## Documentation

Primary docs:

- [`AGENTS.md`](AGENTS.md)
- [`ARCHITECTURE.md`](ARCHITECTURE.md)
- [`docs/index.md`](docs/index.md)
- [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md)
- [`docs/OPERATIONS.md`](docs/OPERATIONS.md)
- Whole-system architecture: [`../docs/system-architecture.md`](../docs/system-architecture.md)

## Features

- **LLM Proxy**: Unified interface for OpenAI, Anthropic, and Gemini (Google).
- **Streaming Support**: NDJSON-based streaming for provider generations.
- **MCP Tool Broker**: Execute tools via Model Context Protocol (MCP) servers.
- **Security**:
  - JWT authentication with JWKS validation.
  - Multi-tenant isolation for secrets and tools.
  - Pluggable secret storage: encrypted Postgres (AES-256-GCM) or HashiCorp Vault (KV v2).
- **Observability**: Structured JSON logging and Prometheus metrics.
- **Resilience**: Configurable timeouts, bounded retries for idempotent outbound dependency calls, and circuit breakers for repeated provider, MCP, and Vault failures.

## Architecture

```mermaid
flowchart LR
    EE[Execution Engine] -->|Bearer JWT + JSON| GW["llm-gateway"]
    GW --> AUTH[Auth & Policy]
    AUTH --> LLM[LLM Proxy Module]
    AUTH --> MCP[MCP Tool Broker]
    LLM --> OPENAI[OpenAI]
    LLM --> ANTH[Anthropic]
    LLM --> GEMINI[Gemini]
    MCP --> REG[Tool Registry]
    MCP --> MOCK[Mock MCP Server]
    GW --> OBS[Logs & Metrics]
    GW --> SEC[Secret Store]
```

## Setup & Development

### Prerequisites

- Python 3.12.11. Local development, CI, and production images are standardized on the same Python patch release.
- Docker & Docker Compose
- Postgres (if running locally without Docker)
- Redis (if running locally without Docker)

The repository pins `.python-version` to `3.12.11` for pyenv-compatible local workflows. Recreate the virtualenv after changing Python versions:

```bash
pyenv install 3.12.11
pyenv local 3.12.11
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install '.[test]' -c constraints.txt
task validate
```

### Compose Layout

- `docker-compose.yml`: base/default component runtime (`gateway`, `postgres`, `redis`, migration job). It is useful for local integration and image smoke tests; full deployment topology lives in `acornops-deployment`.
- `docker-compose.override.yml`: local development additions (local builds, host ports, `mock-auth`, `mock-mcp`, seed data).
- base compose defaults `AUTH_JWKS_URL` to `https://api.<BASE_DOMAIN>/api/v1/auth/jwks.json` (`BASE_DOMAIN=acornops.dev` by default), gates the gateway on `/ready`, and uses `LLM_GATEWAY_IMAGE` for explicit image selection.

### Secret Backends

- Default backend: `SECRETS_BACKEND=database`.
- Optional Vault backend: set `SECRETS_BACKEND=vault` and provide:
  - `VAULT_ADDR`
  - `VAULT_TOKEN`
  - optional `VAULT_NAMESPACE`
  - optional `VAULT_MOUNT` (default `secret`)
  - optional `VAULT_PATH_PREFIX` (default `acornops`)
  - optional `VAULT_TIMEOUT_MS` / `VAULT_VERIFY_TLS`
- LLM provider credentials are workspace-scoped and are configured through
  workspace AI settings.
- MCP auth secrets remain target-scoped by `workspace_id`, `target_id`, and
  `target_type`.
- For local Vault testing, the override includes a `vault` service under the `vault` profile:

```bash
docker compose --profile vault up -d --build
```

### Internal MCP Admin API

The control-plane manages target-scoped MCP and built-in tool configuration through llm-gateway internal endpoints. Kubernetes callers pass `target_type: "kubernetes"`:

- `GET /api/v1/internal/mcp/servers`
- `GET /api/v1/internal/mcp/tools`
- `PATCH /api/v1/internal/mcp/tools/{tool_name}`
- `POST /api/v1/internal/mcp/servers`
- `PATCH /api/v1/internal/mcp/servers/{server_id}`
- `POST /api/v1/internal/mcp/servers/{server_id}/test`
- `DELETE /api/v1/internal/mcp/servers/{server_id}`

All of the above require `Authorization: Bearer <ADMIN_API_TOKEN>` and explicit `workspace_id` + `target_id` + `target_type` query/body scope where applicable.

### MCP Server Discovery Contract

For remote MCP servers managed by the management console's MCP Servers tab:

- `server_url` is the single MCP Streamable HTTP endpoint (for example,
  `https://mcp.example.com/mcp`). The gateway does not append REST-style
  `/tools/list` or `/tools/call` paths.
- each discovery or tool-call operation performs the standard MCP lifecycle:
  `initialize`, `notifications/initialized`, then `tools/list` or `tools/call`.
- the client negotiates the protocol version, returns an issued
  `MCP-Session-Id`, sends `MCP-Protocol-Version`, and accepts both JSON and SSE
  responses.
- each operation uses an isolated session and asks the server to terminate it
  on close. Idempotent discovery reinitializes once after explicit session
  termination; tool calls are never automatically replayed.

On server create (when no explicit tool list is supplied), llm-gateway discovers
tools, stores them disabled, and sanitizes remote descriptions/schemas before
they are shown for admin review. A discovered external tool is not returned to
runtime tool lists or sent to an LLM until an admin explicitly enables it with a
reviewed capability.
Each MCP server also tracks `connection_status`, `last_discovery_at`, and `last_discovery_error` so UI can surface discovery health.
If discovery fails or returns empty, server creation still succeeds but no tools are mapped until discovery succeeds.
MCP server `public_headers` are visible non-secret metadata. Credential-bearing headers must use secret-backed auth fields.

MCP egress is protected by default. Remote MCP URLs must be absolute HTTP(S) URLs without embedded credentials; production requires HTTPS and rejects DNS results in loopback, link-local, multicast, private, reserved, or unspecified address ranges. Local development allows Docker service-name targets such as `mock-mcp`. Private production MCP targets require an exact `MCP_EGRESS_ALLOWED_HOSTS` allowlist entry or `MCP_EGRESS_ALLOW_PRIVATE_NETWORKS=true`; neither setting bypasses HTTPS or certificate verification. Mount an organization private CA and set `MCP_EGRESS_CA_BUNDLE_FILE` to its PEM bundle; this extends normal trust only for generic remote MCP traffic. The configured AcornOps builtin bridge URL uses a separate trusted internal transport and is selected only when control plane registers tools with `source: builtin`.

LLM and tool-call limits are configured by `LLM_RATE_LIMIT_PER_WINDOW`, `TOOL_RATE_LIMIT_PER_WINDOW`, and `RATE_LIMIT_WINDOW_SECONDS`. In production, `REQUIRE_REDIS_RATE_LIMITS_IN_PRODUCTION=true` makes missing Redis configuration a startup error.

Runtime JWKS checks are configured with `JWKS_CACHE_TTL_SECONDS`,
`JWKS_READINESS_MAX_STALENESS_SECONDS`, and `REQUIRE_JWKS_READINESS`. Production
defaults require JWKS readiness so gateway instances only receive traffic when
they can validate control-plane-issued run tokens.

Tool registry and secret values are cached in-process. When Redis is configured,
tool registry and secret writes publish cache invalidation events for other
gateway instances. Without Redis, propagation delay is bounded by
`TOOL_REGISTRY_CACHE_TTL_SEC` and `SECRETS_CACHE_TTL_SEC`. Production requires
`SECRETS_CACHE_TTL_SEC=0` so plaintext provider secrets are not retained in the
gateway process cache.

### Run Modes

1. Component-only local development (recommended in this repo):

```bash
docker compose up -d --build
```

This starts:
- gateway (`http://localhost:8001`)
- postgres (`localhost:5432`)
- redis (`localhost:6379`)
- mock-auth (`http://localhost:8003`)
- mock-mcp (`http://localhost:8002/mcp`)

The gateway and mock auth/MCP services run with Uvicorn `--reload`, so code changes are reflected immediately.

2. Component-only image smoke test:

```bash
docker compose -f docker-compose.yml up -d
```

Use `LLM_GATEWAY_IMAGE=ghcr.io/acornops/llm-gateway:<release>` to select a release image. This compose file does not replace the full AcornOps deployment manifests.

3. Full AcornOps stack (all components together):

```bash
cd ../acornops-deployment
task local-up
```

This full-stack flow uses the deployment repo `Taskfile.yml` and requires the `task` CLI to be installed.

Use full-stack mode when you want to validate control-plane-issued auth and real end-to-end traffic.
Do not run this repository's local compose stack and `acornops-deployment` local stack at the same time on the same host ports.

If dependencies change (`requirements.txt` or `requirements.lock`), rebuild once:

```bash
docker compose up -d --build
```

### Local Development (Docker Required)

Docker is required for local development and testing to ensure consistency with the production environment.

Optional re-seed command:

```bash
docker compose run --rm gateway-init sh -c "alembic upgrade head && python scripts/seed_db.py"
```

To test real inference in local development, set dev seed provider API keys before
seeding. The seed job stores non-blank values as workspace-scoped provider
credentials; production traffic should configure credentials through workspace
settings. For providerless deterministic smoke runs,
`LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES=true` makes the seed job write fake
local-only provider keys so upstream services can exercise credential preflight
without calling a real provider. Gemini is the recommended demo default:

```bash
export ACORNOPS_DEV_SEED_GEMINI_API_KEY='<your-gemini-api-key>'
docker compose run --rm gateway-init sh -c "alembic upgrade head && python scripts/seed_db.py"
```

The Gemini adapter uses Google's current `google-genai` SDK.

## Image Consumption

The gateway publishes non-root container images for the broader AcornOps
deployment. In production, consume those images through `acornops-deployment`
and pin a release tag instead of using a mutable tag. Release builds publish
SBOM/provenance metadata through GitHub Actions. The gateway image installs from
the hash-locked `requirements.lock` file.

### 1. Pull the Image (Optional)
Images are built and pushed to GHCR by the release workflow.
```bash
docker pull ghcr.io/acornops/llm-gateway:<release-tag>
```

### 2. Local Integration Example
For local integration testing outside the full AcornOps deployment, use an
explicit release tag in compose:

```yaml
services:
  gateway:
    image: ghcr.io/acornops/llm-gateway:<release-tag>
    ports:
      - "8001:8001"
    environment:
      - DATABASE_URL=postgresql+asyncpg://gateway_user:gateway_password@postgres:5432/gateway
      - REDIS_URL=redis://redis:6379/0
      - SECRETS_KEK_BASE64=SglIGBscu1EgQ+AlpqJLADNN9QmCzS9d1ZvK3oT/e5s=
      - AUTH_JWKS_URL=http://mock-auth:8003/jwks.json
      - AUTH_ISSUER=llm-gateway
      - AUTH_AUDIENCE=execution-gateway
    depends_on:
      - postgres
      - redis

  postgres:
    image: postgres:16
    environment:
      - POSTGRES_USER=gateway_user
      - POSTGRES_PASSWORD=gateway_password
      - POSTGRES_DB=gateway

  redis:
    image: redis:7-alpine

  mock-auth:
    build:
      context: ../llm-gateway
      dockerfile: deployments/Dockerfile.mock-auth
    ports:
      - "8003:8003"
```

### 3. Point a Local Client to the Gateway
Configure a local client to use the gateway's address:
```python
client = GatewayLlmClient(url="http://gateway:8001", token="your-jwt-token")
```

### 4. Database Setup in Integration
Remember to run the migrations and seed the database in your integration environment, or mount a pre-seeded volume to Postgres.

## Testing

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=app
```

### Manual Testing

1. **Get a Mock Token**:
   The `mock-auth` service runs on port 8003:
   ```bash
   curl -X POST http://localhost:8003/token \
     -H "Content-Type: application/json" \
     -d '{
       "run_id": "5e709a9c-2481-4baa-aec2-ca193c50167d",
       "workspace_id": "4b930d98-add9-4924-ab26-3c16d96ec373",
       "target_id": "5b006e4c-509c-458a-9f02-5aafbdc01ade",
       "target_type": "kubernetes",
       "session_id": "6e30d188-7e4f-4cce-a368-40b34004d725"
     }'
   ```

2. **Stream Provider Generations**:
   ```bash
   curl -X POST http://localhost:8001/api/v1/llm/generations:stream \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{
       "run_id": "5e709a9c-2481-4baa-aec2-ca193c50167d",
       "workspace_id": "4b930d98-add9-4924-ab26-3c16d96ec373",
       "target_id": "5b006e4c-509c-458a-9f02-5aafbdc01ade",
       "target_type": "kubernetes",
       "session_id": "6e30d188-7e4f-4cce-a368-40b34004d725",
       "provider": "gemini",
       "model": "gemini-2.0-flash",
       "messages": [{"role": "user", "content": "Hello!"}]
     }'
   ```

3. **Call a Tool**:
   ```bash
   curl -X POST http://localhost:8001/api/v1/mcp/tool-call \
     -H "Authorization: Bearer <TOKEN>" \
     -H "Content-Type: application/json" \
     -d '{
       "run_id": "5e709a9c-2481-4baa-aec2-ca193c50167d",
       "workspace_id": "4b930d98-add9-4924-ab26-3c16d96ec373",
       "target_id": "5b006e4c-509c-458a-9f02-5aafbdc01ade",
       "target_type": "kubernetes",
       "tool": "get_weather",
       "arguments": {"location": "San Francisco"}
     }'
   ```

## API Documentation

Once the server is running, visit:
- Swagger UI: `http://localhost:8001/docs`
- ReDoc: `http://localhost:8001/redoc`
- OpenAPI JSON: `http://localhost:8001/openapi.json`

`ENABLE_API_DOCS` controls docs exposure. Local override sets it to `true`; base/production-style compose defaults it to `false`.

## Observability

- **Metrics**: `http://localhost:8001/metrics` (Prometheus format)
- **Logs**: JSON formatted logs emitted to stdout.
- **Liveness**: `http://localhost:8001/health` returns process health only.
- **Readiness**: `http://localhost:8001/ready` verifies database connectivity,
  Redis connectivity when Redis-backed features are enabled, JWKS reachability
  and cache freshness, and secret-backend health. It returns `503` with
  per-dependency details when the gateway should not receive traffic.

## Validation

Run the checks that match the change:

- `task python:check`
- `task contracts:check`
- `task harness:check`
- `task lint`
- `task validate`
- `task unit-test` in a provisioned environment when auth, provider, or MCP behavior changes
