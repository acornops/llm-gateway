# LLM Gateway Reliability

## Failure Modes

- JWT validation or admin-token validation regresses.
- Provider streaming shape changes break normalized NDJSON output.
- MCP registry state or transport fallbacks drift from expected behavior.
- Secret backend or database state becomes unavailable mid-request.

## Required Validation

- Run `task python:check`, `task contracts:check`, and `task harness:check` for all substantive changes.
- Run `task lint` in all environments.
- Run `task unit-test` in a provisioned Python 3.12.11 environment when auth, provider, or MCP behavior changes.
- Preserve normalized stream event and tool-call response shapes.

## Recovery Expectations

- Prefer explicit scope/auth errors over silent fallback.
- Keep provider-specific quirks contained behind adapters.
- Capture newly discovered runtime invariants in docs or checks when they become durable.
- Treat `/health` as liveness only and `/ready` as the operator-facing dependency gate.
- `/ready` must fail when the database is unavailable, when required JWKS refresh/cache
  health is unavailable, when the configured secret backend is unavailable, and on Redis
  connectivity when Redis-backed features are enabled.

## Outbound Dependency Resilience

- Provider streaming retries are allowed only before the gateway has emitted any NDJSON event for a request.
- MCP tool discovery and Vault secret reads use bounded exponential backoff for retryable timeout, connection, rate-limit, and 5xx failure modes.
- MCP tool execution does not automatically retry because tool side effects may be non-idempotent; repeated retryable failures instead trip a short-lived circuit breaker.
- Provider, MCP, and secret-backend circuit breakers open after repeated retryable failures and short-circuit subsequent calls until the cooldown expires.
- Dependency retry, failure, and circuit-open events must be visible through structured logs or Prometheus metrics.
- Tool registry entries and secrets are cached in-process. In production, keep Redis
  enabled so tool registry and secret writes publish cross-instance invalidation
  events. Production must keep `SECRETS_CACHE_TTL_SEC=0`; otherwise gateway
  startup fails to prevent plaintext secret retention in process memory.
- Provider adapters should use actively supported SDKs. Gemini uses
  `google-genai`; do not reintroduce the deprecated `google-generativeai`
  package.
