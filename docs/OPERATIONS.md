# LLM Gateway Operations

## Runtime Contract

- `GET /health` is liveness only.
- `GET /ready` gates production traffic and checks database, Redis, JWKS readiness, and the configured secret backend.
- API docs must stay disabled in production unless deliberately enabled for a private environment.
- The gateway should not be publicly exposed by default; browser and execution traffic should flow through the control plane and internal service network.

## Required Environment

- `APP_ENV=production`
- `DATABASE_URL`
- `REDIS_URL`
- `AUTH_JWKS_URL`
- `ADMIN_API_TOKEN`
- `SECRETS_BACKEND`
- `SECRETS_KEK_BASE64`
- `SECRETS_CACHE_TTL_SEC=0`

Vault-backed deployments additionally require:

- `VAULT_ADDR`
- `VAULT_TOKEN`
- `VAULT_VERIFY_TLS=true`

## Provider Endpoint Overrides

Provider SDKs use their public hosted endpoints by default. A platform operator
can redirect all workspaces to API-compatible endpoints with these optional
environment variables:

- `LLM_PROVIDER_OPENAI_BASE_URL`
- `LLM_PROVIDER_ANTHROPIC_BASE_URL`
- `LLM_PROVIDER_GEMINI_BASE_URL`

Set each value to the fully qualified API base URL expected by that provider's
SDK. The endpoint must implement the native API used by the gateway: OpenAI
Responses, Anthropic Messages, or Google GenAI GenerateContent. An endpoint that
only implements OpenAI Chat Completions is not sufficient. API keys remain
workspace-scoped; endpoint overrides apply to the entire gateway deployment.

## Migration Operations

Run migrations before starting upgraded application code:

```bash
alembic upgrade head
```

Kubernetes deployments run this through the Helm migration Job.

## Failure Modes

- Readiness fails on database: verify `DATABASE_URL`, credentials, network policy, and migration state.
- Readiness fails on Redis: verify `REDIS_URL`; production rate limits fail closed when Redis is required.
- Readiness fails on JWKS: verify the control-plane JWKS URL and signing key availability.
- Secret backend failures: verify `SECRETS_BACKEND`, KEK material, or Vault connectivity depending on the configured backend.

## Required Validation

Before release or deployment chart changes:

```bash
task validate
```
