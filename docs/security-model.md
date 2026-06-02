# LLM Gateway Security Model

## Trust Boundaries

- Runtime JWT validation defines per-run scope.
- Admin APIs use a separate shared admin token value.
- Provider secrets and MCP server secrets are sensitive infrastructure state.
- The gateway must not infer broader permissions than the token or config provide.

## Secrets

- Never log provider API keys, MCP auth secrets, or bearer tokens.
- Keep issuer/audience configuration aligned with control-plane token issuance.
- Treat secret-backed MCP auth material as sensitive configuration. MCP `public_headers` are visible non-secret metadata and must reject credential-like names.
- Validate MCP auth header names and constructed header values before outbound forwarding so malformed secrets cannot inject additional headers.
- Production must run with `SECRETS_CACHE_TTL_SEC=0`; the gateway rejects
  production startup otherwise to avoid retaining plaintext provider secrets in
  process memory.
- Production must keep `LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES=false`; the
  gateway rejects production startup when deterministic local smoke responses
  are enabled.
- When Redis is configured, provider secret writes publish cache invalidation events
  so other gateway instances stop using stale in-process values without waiting for
  `SECRETS_CACHE_TTL_SEC`.

## MCP Egress

- Remote MCP server URLs are validated before registration and before outbound calls.
- Production requires HTTPS by default and blocks loopback, link-local, multicast, private, reserved, and unspecified IP ranges after DNS resolution.
- Local development may use short Docker service names such as `mock-mcp`; production private/internal MCP servers require an explicit `MCP_EGRESS_ALLOWED_HOSTS` entry or `MCP_EGRESS_ALLOW_PRIVATE_NETWORKS=true`.
- Do not allow private MCP egress in public SaaS deployments unless the network boundary and tenant ownership model are explicitly reviewed.

## Rate Limits

- LLM and tool-call rate limits are configurable with `LLM_RATE_LIMIT_PER_WINDOW`, `TOOL_RATE_LIMIT_PER_WINDOW`, and `RATE_LIMIT_WINDOW_SECONDS`.
- Production defaults fail closed when Redis-backed rate limiting is required but `REDIS_URL` is missing.

## High-Risk Changes

- JWT validation, service-token validation, or claim models
- Provider adapter request/response normalization
- MCP server auth and header forwarding
- Secret backend or migration behavior
