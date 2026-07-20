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
These `LLM_PROVIDER_*_BASE_URL` names are the only supported AcornOps endpoint
configuration. The gateway passes an explicit URL to each SDK, so ambient SDK
variables such as `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`, and
`GOOGLE_GEMINI_BASE_URL` do not silently alter provider routing.

Provider attempts that fail emit `provider_stream_failed` with the provider,
model, run and workspace identifiers, sanitized base URL, attempt counters,
base URL source, additional-CA state, outer exception type, root-cause type,
HTTP status when available, and one of these bounded `error_category` values.
Logged URLs exclude user information, query strings, and fragments. The
`base_url_source` field distinguishes an AcornOps setting from the provider
default. Error categories are:

- `tls_certificate_verification`
- `tls`
- `dns`
- `connect`
- `timeout`
- `http_4xx`
- `http_5xx`
- `http_other`
- `other`

Production logs contain bounded, credential-redacted error summaries. Endpoint
URLs and raw errors are not placed in metric labels, and client-facing stream
errors remain deliberately generic.

## Remote MCP Connectivity

Configure each remote server with its single Streamable HTTP endpoint, normally
an HTTPS URL ending in `/mcp`. The server must support the MCP initialization
lifecycle and `tools/list` / `tools/call` JSON-RPC methods at that endpoint.

Production blocks private DNS results by default. For an organization-internal
server, prefer the exact hostname allowlist:

```env
MCP_EGRESS_ALLOWED_HOSTS=test-mcp.app.internal.org
MCP_EGRESS_ALLOW_PRIVATE_NETWORKS=false
```

Allowlisting a hostname does not disable HTTPS verification. Install the
organization CA in the gateway image or mount a PEM bundle and set:

```env
ADDITIONAL_CA_BUNDLE_FILE=/etc/acornops/trust/additional-ca.pem
```

Do not use `MCP_EGRESS_ALLOW_PRIVATE_NETWORKS=true` unless all private-network
destinations are within the deployment's trust boundary. Host allowlisting is
exact; wildcard suffixes are not supported.

This bundle extends normal public trust for all gateway outbound TLS clients,
including providers, JWKS, Vault, remote MCP, `rediss://`, and explicitly
TLS-enabled PostgreSQL. It does not enable TLS for plaintext dependency URLs.
For PostgreSQL through asyncpg, use `ssl=verify-full` or
`sslmode=verify-full`; the gateway normalizes either form before connecting.

The remote client accepts standard JSON and SSE responses, caps each response
at `MCP_MAX_TOOL_RESULT_BYTES`, and rejects compressed responses so the limit is
enforced before decoding. Upstream HTTP logs include only bounded, sanitized
error messages and never MCP session IDs or configured header values.

`REMOTE_MCP_ENABLED=false` is the emergency kill switch. It blocks remote MCP
discovery and execution without changing installation or credential state, and does
not block the platform-owned built-in MCP bridge. Remote MCP reachability is
not a `/ready` dependency. Use `MCP_CONNECTION_RATE_LIMIT_PER_WINDOW` to set the
shared connect/verify attempt budget for each credential owner and installation.

## Migration Operations

Run the greenfield baseline before starting application code:

```bash
alembic upgrade head
```

Kubernetes deployments run this through the Helm migration Job.

The credential-ownership schema creates only the final generic connection
model. Recreate every pre-release gateway database and secret namespace; no
credential-reset or superseded connection conversion runs. Release the baseline
and application together because mixed-version operation is unsupported.

Release in a maintenance window:

1. Stop new run admission and automation schedulers, then drain active runs.
2. Set `REMOTE_MCP_ENABLED=false` and confirm built-in tools still work.
3. Back up the gateway database and secret namespace.
4. Deploy the gateway migration and application, then the control plane,
   management console, deployment configuration, and documentation.
5. Smoke test target and Agent workspace/individual credential
   connect/verify/disconnect, exact-tool
   readiness, and built-in tools before setting `REMOTE_MCP_ENABLED=true`.

After a new credential is entered, keep remote MCP disabled and forward-fix;
restoring obsolete auth data can send it to the wrong trust boundary.

## MCP credential rotation and revocation

Use least-privilege provider credentials that expose only the tools required by
the run. Replace the credential through the installation connection dialog, then Verify. A
failed rotation keeps the new credential in error state and clears its tool snapshot;
repair the provider-side grant and Verify again, or replace/disconnect it.
Runtime 401/403 responses do the same and subsequent calls fail before
contacting upstream. Changing an installation URL, auth type, header name, or
prefix invalidates every credential connection for that installation; metadata-only catalog reimports
do not.

Remote MCP credentials are unrelated to platform OIDC. OIDC signs users into
AcornOps; it does not authorize remote MCP calls. Workspace-managed mode can use
a service or bot credential; individual mode requires a user credential.

## Failure Modes

- Readiness fails on database: verify `DATABASE_URL`, credentials, network policy, and migration state.
- Readiness fails on Redis: verify `REDIS_URL`; production rate limits fail closed when Redis is required.
- Readiness fails on JWKS: verify the control-plane JWKS URL and signing key availability.
- Secret backend failures: verify `SECRETS_BACKEND`, KEK material, or Vault connectivity depending on the configured backend.
- Provider failures: inspect `provider_stream_failed`. Certificate-chain or
  hostname failures use `error_category=tls_certificate_verification` and expose
  the wrapped SDK root cause without logging provider credentials.
- Remote MCP registration fails with an egress error: verify the exact hostname
  allowlist, DNS result, HTTPS URL, and private CA trust configuration.
- Remote MCP registration reports a protocol error: verify that the URL is the
  single Streamable HTTP endpoint and that the server accepts `initialize`
  before `tools/list`.

## Required Validation

Before release or deployment chart changes:

```bash
task validate
```
