# LLM Gateway Security Model

## Trust Boundaries

- Runtime JWT validation defines per-run scope.
- Admin APIs use a separate shared admin token value.
- Provider secrets and MCP server secrets are sensitive infrastructure state.
- The gateway must not infer broader permissions than the token or config provide.

## Secrets

- Never log provider API keys, MCP auth secrets, or bearer tokens.
- MCP credentials are write-only and encrypted by the configured secret backend.
  Workspace-managed credentials are isolated by installation; individual
  credentials are isolated by workspace user plus installation. They are never
  copied or compared across target and Agent installations.
- Keep issuer/audience configuration aligned with control-plane token issuance.
- Treat secret-backed MCP auth material as sensitive configuration. MCP `public_headers` are visible non-secret metadata and must reject credential-like names.
- Validate MCP auth header names and constructed header values before outbound forwarding so malformed secrets cannot inject additional headers.
- Derive credential header names and prefixes from the installation; clients
  cannot choose outbound credential formatting when they connect.
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
- Explicit integration-test profiles may use test-only Docker service names; production private/internal MCP servers require an exact `MCP_EGRESS_ALLOWED_HOSTS` entry or `MCP_EGRESS_ALLOW_PRIVATE_NETWORKS=true`.
- A private-network allowlist never bypasses production HTTPS or certificate
  verification. Organization private CAs must be installed in the gateway
  trust store or supplied with `ADDITIONAL_CA_BUNDLE_FILE`; the latter extends
  normal trust for every gateway outbound TLS dependency.
- Generic remote connections pin the validated DNS address while preserving the
  original Host header and TLS server name. Redirects and compressed responses
  are rejected.
- MCP lifecycle headers (`MCP-Session-Id`, `MCP-Protocol-Version`, transport
  `Accept` headers, and `Last-Event-ID`) are platform-owned and cannot be
  supplied through public or secret-backed custom headers.
- Remote sessions are isolated per operation, terminated on close, and never
  logged. Bounded upstream errors are sanitized against all outbound header
  values before logging.
- The configured target-adapter bridge URL is allowed as internal HTTP only when a live target registration supplies builtin tools and no configurable MCP auth or public headers.
- Do not allow private MCP egress in public SaaS deployments unless the network boundary and tenant ownership model are explicitly reviewed.

## Rate Limits

- LLM and tool-call rate limits are configurable with `LLM_RATE_LIMIT_PER_WINDOW`, `TOOL_RATE_LIMIT_PER_WINDOW`, and `RATE_LIMIT_WINDOW_SECONDS`.
- Production defaults fail closed when Redis-backed rate limiting is required but `REDIS_URL` is missing.

## High-Risk Changes

- JWT validation, service-token validation, or claim models
- Provider adapter request/response normalization
- MCP server auth and header forwarding
- Secret backend or migration behavior
