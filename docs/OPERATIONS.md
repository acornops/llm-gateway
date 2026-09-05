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

The Vault token is a KV-v2 token. Grant `read`, `create`, and `update` on
`<mount>/data/<VAULT_PATH_PREFIX>/+/_global/*`; `list` on
`<mount>/metadata/<VAULT_PATH_PREFIX>/+/_global`; and `delete` on
`<mount>/metadata/<VAULT_PATH_PREFIX>/+/_global/*`. Vault policy uses `+` for
the single workspace path segment; `*` is reserved for the trailing key suffix.
Readiness performs a shaped
LIST of `<mount>/metadata/<VAULT_PATH_PREFIX>/_readiness/_global` and accepts an
authorized 404, so grant `list` on that sentinel path too. Readiness does not
prove the broader maintenance permission described below. A 403 on the sentinel
makes the gateway unready.

## Provider Endpoint Overrides

Provider SDKs use their public hosted endpoints by default. A platform operator
can redirect all workspaces to API-compatible endpoints with these optional
environment variables:

- `LLM_PROVIDER_OPENAI_BASE_URL`
- `LLM_PROVIDER_OPENAI_API_SURFACE=responses|chat_completions`
- `LLM_PROVIDER_ANTHROPIC_BASE_URL`
- `LLM_PROVIDER_GEMINI_BASE_URL`

Set each value to the fully qualified API base URL expected by that provider's
SDK. Anthropic endpoints must implement Messages and Gemini endpoints must
implement Google GenAI GenerateContent. OpenAI uses the Responses API by default;
set `LLM_PROVIDER_OPENAI_API_SURFACE=chat_completions` only when the selected
endpoint implements Chat Completions. API keys remain workspace-scoped, while
endpoint and OpenAI API-surface overrides apply to the entire gateway deployment.
These `LLM_PROVIDER_*_BASE_URL` names and
`LLM_PROVIDER_OPENAI_API_SURFACE` are the only supported AcornOps provider-route
configuration. The gateway passes an explicit URL to each SDK, so ambient SDK
variables such as `OPENAI_BASE_URL`, `ANTHROPIC_BASE_URL`, and
`GOOGLE_GEMINI_BASE_URL` do not silently alter provider routing.

OpenAI API-surface selection is explicit. The gateway does not probe or
automatically fall back between `/responses` and `/chat/completions`. Both
surfaces normalize text streaming, custom function calls, usage, retries, and
provider failures into the same AcornOps events. Chat Completions rejects the
AcornOps native-tool contract before provider dispatch and reports requested
reasoning summaries as unavailable. Restore `responses` to roll back the
alternate API surface. Chat Completions requires the current
`max_completion_tokens` and streamed-usage contract; deprecated or partial
endpoint behavior is not silently accepted.

Provider attempts that fail emit `provider_stream_failed` with the provider,
model, run and workspace identifiers, sanitized base URL, attempt counters,
base URL source, the OpenAI API surface when applicable, additional-CA state,
outer exception type, root-cause type, HTTP status when available, and one of
these bounded `error_category` values.
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

Individual-user MCP OAuth is enabled by default and can be disabled with
`MCP_OAUTH_ENABLED=false`. Configure `MCP_OAUTH_PUBLIC_CONSOLE_URL` with the
canonical public HTTPS origin that serves the console and its same-origin
`/api` proxy. OAuth callbacks and CIMD metadata use that origin so host-only
browser session cookies remain available without being shared across
subdomains. Provide both `REDIS_URL` and the normal encrypted secret backend.
Production readiness fails closed when OAuth is enabled without Redis.
Authorization servers are never readiness dependencies.

PostgreSQL advisory locks use pools separate from lifecycle/connection state
queries so a lock holder cannot starve the data session needed to finish. Each
gateway replica reserves bounded, no-overflow pools of 5 lifecycle server/scope
locks, 2 long-running user-lifecycle locks, 5 connection-owner locks, and 2 OAuth
registration locks: at most 14 additional database connections per replica.
Include these alongside the ordinary store pools in PostgreSQL connection-budget
and replica-capacity calculations. The pinned control-plane lifecycle worker
concurrency and claim limit must remain at or below 2. That matches the
long-running user-lifecycle pool, so a queued cleanup does not spend its
120-second request budget waiting for a gateway lock connection.

OAuth discovery, registration, token, refresh, and revocation requests use the
same MCP egress allowlists, private-address restrictions, DNS pinning, and
custom CA policy as remote MCP traffic. Each endpoint is validated
independently. Requests do not follow redirects, accept compression, reuse
cookies, or carry credentials from another request. Response buffering is
bounded by `MCP_OAUTH_MAX_RESPONSE_BYTES`; request timeout is controlled by
`MCP_OAUTH_HTTP_TIMEOUT_MS`. Flow state defaults to 600 seconds via
`MCP_OAUTH_FLOW_TTL_SECONDS`, and refresh starts inside the
`MCP_OAUTH_REFRESH_SAFETY_SECONDS` window.

## Migration Operations

Run the greenfield baseline before starting application code:

```bash
alembic upgrade head
```

Kubernetes deployments run this through the Helm migration Job.

The credential-ownership schema creates only the final generic connection
model. Migration `c3006973a8d2` and control-plane migration `006` add the
generation-bound user lifecycle. The pinned offline reset intentionally
disconnects every existing *individual* MCP connection and removes its OAuth
authorization/flow state; users must reconnect or reauthorize afterward.
Workspace-owned installation credentials are not reset. This is an accepted
pre-production one-time reset, not a legacy-secret adoption path. Mixed-version
operation is unsupported.

Release MCP endpoint-invariant changes in a maintenance window. Gateway and
control-plane versions that use the dedicated built-in synchronization endpoint
are a pinned pair; do not roll either side independently.

1. Notify users with this wording before the window: "Existing per-user MCP
   connections will be disconnected during maintenance. Reconnect credentials
   and reauthorize OAuth MCP servers afterward. Shared workspace MCP
   connections are unaffected by the membership-generation reset, but any
   installation identified by the endpoint-mismatch report will also have its
   shared credential revoked and must be reconnected." Stop new run admission and automation
   schedulers, then drain active runs.
2. Set `REMOTE_MCP_ENABLED=false` and confirm built-in tools still work.
3. Let the old control-plane secret-cleanup worker drain. Before stopping the
   old pair, require this query to return zero. Control-plane migration `006`
   fails closed when it is nonzero and then drops this legacy queue table.

   ```sql
   SELECT COUNT(*) AS legacy_mcp_cleanup_jobs FROM mcp_secret_cleanup_jobs;
   ```

4. Using the new gateway maintenance image with the production database in
   read-only/report mode, inventory endpoint mismatches and duplicate built-in
   destinations without changing state:

   ```bash
   python -m app.scripts.mcp_endpoint_mismatch_preflight
   ```

   Treat every mismatched non-built-in installation as a trust-boundary event,
   including credential-free servers: enabled/reviewed tool definitions may have
   been discovered at the copied tool URL rather than the owning server URL.
   Record its workspace/server IDs in the incident channel. For each duplicate
   built-in destination, inspect control-plane MCP
   references and retained run snapshots and identify exactly one canonical
   server ID. Do not proceed if different siblings are still authoritative in
   control-plane state; reconcile those references through the approved
   maintenance process first.
5. Quiesce and scale every old control-plane API/worker and llm-gateway replica
   to zero. Verify no old pod, VM process, scheduler, or leased cleanup worker
   remains. Back up both service databases and the gateway secret namespace.
   State-changing preflight cleanup is forbidden before this scale-to-zero
   boundary because an old writer could recreate credentials after its snapshot.
6. While both old services remain stopped, use the new maintenance image to
   clean every mismatched installation and explicitly deduplicate each reported
   built-in destination. Build one apply invocation containing a repeated
   `--canonical-builtin-server-id` flag for every operator-verified destination;
   do not invoke them sequentially because an intermediate run correctly exits
   nonzero while another duplicate remains:

   ```bash
   python -m app.scripts.mcp_endpoint_mismatch_preflight \
     --apply-cleanup \
     --canonical-builtin-server-id <verified-server-id-1> \
     --canonical-builtin-server-id <verified-server-id-2>
   python -m app.scripts.mcp_endpoint_mismatch_preflight \
     --fail-on-active-connections \
     --fail-on-duplicate-builtins
   ```

   This pre-c300 maintenance path deliberately uses only legacy-schema columns.
   It first durably fences every mismatched non-built-in server, including those
   with no connection rows, then revokes OAuth where supported, removes OAuth
   flow/registration state, deletes secret-backed credentials, and deletes
   connection and explicitly selected duplicate server rows. It is safe to retry
   after a partial external failure, including when no connection rows remain.
   An apply run exits nonzero whenever any mismatched non-built-in server, any
   credential row, or any duplicate destination remains. Migration b200
   independently rejects every remaining non-built-in mismatch, even when the
   server is fenced. After the new pair starts, each fenced server requires
   successful authoritative discovery at its
   canonical endpoint; discovered tools return disabled and pending review before
   operators may approve and enable them. Do not perform ad hoc row deletion
   outside this command.
7. Run gateway migrations `b20058629f4b` and `c3006973a8d2` and control-plane
   migration `006` while both services remain stopped. Use an online migration
   connection; b200 performs data-dependent preflight/backfill and does not
   support offline SQL generation. Do not use a rolling deployment for this
   change. Migration c300 also creates the composite
   `gateway_secrets(tenant_scope,secret_name)` inventory index.
8. With the migrated gateway database and maintenance image, report and then
   apply the explicit offline individual reset before starting either service:

   ```bash
   python -m app.scripts.mcp_user_lifecycle_preflight
   python -m app.scripts.mcp_user_lifecycle_preflight --apply-individual-reset
   ```

   `individualConnectionResetCount` is the user-visible reset count;
   `workspaceOwnedConnectionCountUnaffected` and
   `workspaceOwnedSecretObjectCountUnaffected` are baselines that must be
   identical before and after apply. Apply enumerates every user connection row,
   best-effort revokes OAuth while its token is still reachable, deletes both
   deterministic credential identities and the row, purges all remaining
   user-pattern MCP secrets (including former-member and missing-server state),
   and removes every MCP OAuth preparation/callback/index key. It preserves
   installation-owned rows and secrets, is idempotent after partial failure, and
   exits nonzero unless `individualConnectionResetCount`,
   `individualSecretObjectCount`, and `oauthFlowRecordCount` are all zero.

   Vault deployments must use an audited maintenance token that additionally
   has `list` on `<mount>/metadata/<VAULT_PATH_PREFIX>` so the global reset can
   enumerate every workspace, plus the child LIST and metadata DELETE permissions
   above. Validate both the parent and one representative
   `<workspace>/_global` LIST before apply; normal `/ready` checks only the shaped
   sentinel and cannot prove this elevated maintenance capability. Large Vault
   namespaces are traversed workspace-by-workspace, so record the report duration
   and verify it fits the maintenance job timeout.

   Owner activation still serializes against every server in its workspace to
   drain a mutation that crossed the stage boundary, then performs one final
   workspace/user inventory. Use `knownOwnerServerCleanupUpperBound`, the largest
   per-workspace server count, and measured secret-backend latency to prove one
   reconciliation fits the control plane's 120-second request timeout and lease.
   If it does not, reduce installation count/latency or explicitly increase and
   validate the pinned timeout and lease; a longer window alone is not a remedy.
9. Start only the pinned new gateway/control-plane pair. Keep run admission and
   remote MCP disabled until the user lifecycle backlog is drained. This
   readiness query must return zero; the second query is an observability report
   for any remaining nonsynced rows:

   ```sql
   SELECT COUNT(*) AS blocking_user_lifecycle_rows
   FROM workspace_member_mcp_lifecycle
   WHERE blocks_readiness = true
     AND reconciliation_status <> 'synced';

   SELECT status, reconciliation_status, COUNT(*)
   FROM workspace_member_mcp_lifecycle
   WHERE reconciliation_status <> 'synced'
   GROUP BY status, reconciliation_status;
   ```

   Then require the gateway acceptance check to exit zero and compare both
   workspace-owned baselines with step 8:

   ```bash
   python -m app.scripts.mcp_user_lifecycle_preflight --fail-on-unbound
   ```

   The emitted individual connection, secret-object, and OAuth-flow counts must
   remain zero; a nonzero value is a failed rollout even if another SQL backlog
   check is clear.

   Monitor
   `gateway_mcp_user_lifecycle_connections_drained_total{reason="activation"}`
   and the bounded `mcp_user_lifecycle_activation_drained_credentials` log.
10. Confirm startup built-in reconciliation succeeds, the MCP endpoint mismatch
   query reports zero rows, and no old gateway or control-plane replica remains.
   For each preflight-fenced authenticated installation, issue an idempotent
   server PATCH carrying its current `enabled` value and latest revision to
   complete recovery while remote MCP remains disabled. Its old tool rows have
   already been removed. Deploy the Management Console only after that pair is
   healthy.
11. While run admission remains closed, set `REMOTE_MCP_ENABLED=true`, then smoke
   recover every preflight-fenced credential-free installation with an
   idempotent PATCH carrying its current `enabled` value and latest revision;
   require successful authoritative rediscovery with definitions disabled and
   pending review. Then smoke
   test target and Agent workspace/individual credential and OAuth
   connect/verify/disconnect, exact-tool readiness, remote execution, and built-in
   tools. Tell affected users to reconnect bearer/custom credentials and
   reauthorize OAuth servers; authoritative rediscovery must recreate every
   recovered definition disabled and pending review. Reopen run admission only
   after these checks pass. If a smoke test fails, disable remote MCP again and
   forward-fix.

After b200/c300 and control-plane 006 there is no application-only rollback,
even if both old binaries are available. Before reopening traffic, rollback
requires stopping both new services and atomically restoring both service
database backups plus the matching gateway secret namespace, then starting the
old pinned pair. After any new credential is entered, keep remote MCP disabled
and forward-fix; restoring obsolete auth data can send it to the wrong trust
boundary.

## MCP credential rotation and revocation

Use least-privilege provider credentials that expose only the tools required by
the run. Replace the credential through the installation connection dialog, then Verify. A
failed rotation keeps the new credential in error state and clears its tool snapshot;
repair the provider-side grant and Verify again, or replace/disconnect it.
Runtime 401/403 responses do the same and subsequent calls fail before
contacting upstream. Changing an auth type, credential ownership mode, public
header, header name, or prefix invalidates every credential connection for that
installation. Manual installation URLs are immutable; replace the installation
to change its endpoint.

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
