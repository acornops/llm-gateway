# LLM Gateway Development

## Scope

This repository owns the FastAPI LLM gateway, provider adapters, MCP broker path, secrets backend integration, Alembic migrations, and gateway image. Deployment wiring belongs in `acornops-deployment`.

## Prerequisites

- Python 3.12.11. The local interpreter should match `.python-version`; CI and production images use the same Python patch release.
- Task CLI
- Postgres for migration-backed development
- Redis for production-like rate limit behavior

## Local Development

Create a local virtualenv with Python 3.12.11 before running checks:

```bash
pyenv install 3.12.11
pyenv local 3.12.11
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install '.[test]' -c constraints.txt
```

Run canonical validation:

```bash
task validate
```

Run focused checks:

```bash
task lint
task contracts:check
task harness:check
task unit-test
```

For full-stack local development:

```bash
cd ../acornops-deployment
task local-up
```

## Validation

Run the checks that match the change:

- `task python:check`
- `task contracts:check`
- `task harness:check`
- `task lint`
- `task validate`
- `task unit-test` in a provisioned environment when auth, provider, or MCP behavior changes

## Configuration

Important variables:

- `APP_ENV`
- `DATABASE_URL`
- `REDIS_URL`
- `AUTH_JWKS_URL`
- `AUTH_ISSUER`
- `AUTH_AUDIENCE`
- `ADMIN_API_TOKEN`
- `SECRETS_BACKEND`
- `SECRETS_KEK_BASE64`
- workspace AI Settings for real provider-backed flows; no provider credential
  is created by migrations or local startup
- `docker-compose.integration-test.yml` for explicit mock-MCP integration tests;
  install and discover the server through normal admin APIs. The mock endpoint
  serves HTTPS with an ephemeral certificate generated under
  `.integration-certs/`; integration runners trust that certificate explicitly
  through `MCP_EGRESS_CA_BUNDLE_FILE`.

## Migrations

Alembic migrations live under `alembic/`. Production and Kubernetes deployments run:

```bash
alembic upgrade head
```

Back up production Postgres before upgrades that include migrations.

## Documentation Drift Control

Treat documentation as part of feature acceptance. Update the nearest durable doc in the same change when work changes provider behavior, gateway APIs, MCP behavior, secrets handling, configuration, migrations, deployment behavior, operations, security, or reliability.

If docs are intentionally unchanged, record `Docs impact: none` and the reason in handoff evidence.

## Documentation Harness

Keep `README.md`, `AGENTS.md`, `ARCHITECTURE.md`, `docs/index.md`, this file, and `docs/OPERATIONS.md` in sync when changing repo behavior. `task validate` runs the harness checks.
