# LLM-Gateway Contracts

The LLM gateway normalizes model streaming and MCP tool execution for execution-engine while enforcing control-plane run scope. Keep this README as a boundary brief, not as an endpoint catalog.

## Source Of Truth

- Machine-checked cross-repo coverage lives in `docs/contracts/manifest.json`.
- Handler, model, route, setting, and bridge coverage is enforced by `scripts/check-contracts.py`.
- Endpoint, field, and event catalogs belong in manifests and producer APIs, not in this README.
- This README keeps the behavior agents need to reason about: auth channels, runtime scope, native tools, MCP capability safety, and the built-in bridge.

## Full Platform Matrix

- Management console -> control plane
- Control plane <-> execution-engine
- Control plane <-> llm-gateway
- Control plane <-> agentk
- Execution-engine -> llm-gateway

## Platform Dependency Summary

| Counterpart | Contract Surface | Enforcement |
| --- | --- | --- |
| Execution engine | LLM streaming, MCP tool calls, internal model-only skill loading, deterministic smoke mode | Manifest, FastAPI route, handler, and model checks |
| Control plane | JWKS, admin MCP/provider APIs, run JWT claims, built-in MCP bridge | Manifest, admin handler, settings, and bridge checks |

## Shared Invariants

- Runtime auth uses `Authorization: Bearer <run-scoped-jwt>`.
- Admin auth uses `Authorization: Bearer <ADMIN_API_TOKEN>`.
- Runtime and admin traffic are separate contracts and credentials must not be reused.
- Run JWT claims are authoritative for provider, model, tool, native-tool, max-output, target, workflow, and context scope.
- Requested body scope must match token scope; the gateway must not infer missing scope from UI state or registry state.
- Provider credentials, admin tokens, run JWTs, MCP secret headers, raw reasoning state, and chain-of-thought must not be emitted in responses.
- MCP results normalize to `full_result`, `model_context`, `context_meta`,
  `artifact_eligible`, and `is_error`. Trusted AgentK envelopes must validate as
  `acornops.model-context.v1` plus `acornops.full-tool-result.v1`; untrusted MCP
  metadata can never enable artifact persistence.
  See [Tool Result Normalization](/docs/design-docs/tool-result-normalization.md)
  for trusted-producer and generic-result behavior.

## Execution-Engine Boundary Notes

- The gateway accepts requested built-in native tool policy only through `allowed_native_tools`.
- Callable function names must match `^[A-Za-z_][A-Za-z0-9_-]{0,62}$`; the
  gateway rejects unsafe names before provider dispatch, while provider-safe
  platform aliases remain unchanged across OpenAI, Anthropic, and Gemini.
- Provider failure events may use `MODEL_UNAVAILABLE`, `PROVIDER_AUTH_INVALID`,
  `PROVIDER_RATE_LIMITED`, or `PROVIDER_UNAVAILABLE`. `MODEL_UNAVAILABLE`
  requires an explicit structured provider code; ambiguous provider responses
  remain generic and raw provider messages stay in sanitized logs.
- For Gemini, `web_search` accepts the supported native-tool surface and rejects unsupported domain-filter requests.
- The internal model-only skill tool is `_acornops_load_skill`; the manifest entry is `"internalModelOnlyTools": ["_acornops_load_skill"]`.
- `INTERNAL_MODEL_ONLY_TOOLS`, `is_reserved_internal_tool_name`, and `_validate_stream_tool_names` protect the reserved `_acornops_` namespace.
- Local smoke tests may set `LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES`, but production settings reject deterministic provider responses.

## Control-Plane Boundary Notes

- Config must keep `AUTH_ISSUER` and `AUTH_AUDIENCE` aligned with the control-plane run-token issuer and audience.
- `ADMIN_API_TOKEN` gates internal MCP and provider-credential administration.
- Workspace workflow built-in tool calls are forwarded to the control-plane built-in MCP bridge only after an enabled workspace registry entry identifies the tool as built-in.
- Workspace workflow scope uses `scope.type = "workspace"` and explicit workflow identifiers; ordinary workflow selection does not imply an agent id.
- Target adapters register their live built-in tools against the configured internal bridge URL (the local deployment default is `http://control-plane:8081/internal/v1/mcp`). The server identity comes from the registered target, not a seeded workspace integration.
- Connection readiness accepts enabled tools only when their server and tool identities match. Trusted built-in tools do not require remote MCP review or a personal connection snapshot; remote tools remain review-gated, and personal tools must also exist in the acting user's verified snapshot.
- Built-in bridge calls use `Authorization: Bearer <run-scoped-jwt>`, scope source `run-scoped-jwt-claims`, and call path `POST /internal/v1/mcp/tools/call`.
- Optional `tool_call_id` values are forwarded as `toolCallId` only on this
  trusted built-in bridge, preserving AgentK idempotency while keeping generic
  MCP request bodies unchanged.

## Generic MCP Boundary Notes

- MCP registries use the `mcp_registry_v0_1` adapter over a direct HTTPS base URL. The configured URL is a registry root or path prefix without `/v0.1`, query parameters, fragments, or credentials; the gateway appends `/v0.1`. Connector routing is not available.
- Catalog source list responses expose secret-free source-management capabilities. Omitted authentication on update preserves the stored credential, `auth.type = none` clears it, and bearer or custom-header replacement requires a new write-only credential. URL and authentication changes are probed before persistence, clear stale artifacts, and perform a full synchronization.
- Bootstrap registries are reconciled by display name and are configuration-read-only through APIs, although authorized control-plane callers may synchronize them. Removed bootstrap configuration disables a source instead of deleting its cached snapshot. Disabled sources disappear from browsing immediately; deleting a workspace source removes its cache and registry credential without deleting installed MCP servers.
- Registry availability is per-source operational state and does not participate in global gateway readiness. Synchronization logs and metrics use bounded labels and exclude source credentials, authorization headers, and URL query values.
- Active registry records have an explicit `scope_type` of `agent` or `target`. Target records belong to the selected Cluster or VM generic agent; Agent records belong to the workspace Agent named by `agent_id`.
- Catalog import is a discriminated contract: Agent requests carry `agent_id`
  and optional `target_constraints`; target requests carry `target_id` and
  `target_type` and cannot carry Agent constraints. Duplicate and re-import
  checks include workspace, scope type, and destination identity.
- `auth_scope: personal` is mechanism-neutral installation metadata. In V1 it
  requires one write-only PAT per workspace user and installation. Target and
  Agent installations never share or copy a connection, even when their server
  URLs match; workflows reuse the selected Agent installation and connection.
- The installation derives bearer or custom-header formatting. Connecting or
  rotating a PAT persists it before authenticated tool discovery. A failed
  discovery retains the PAT with `status: error`; the verify endpoint retries
  that stored PAT without returning it.
- Runtime calls fail closed for missing or erroneous connections. Upstream
  401/403 responses mark the connection erroneous. Personal MCP calls reject
  service principals with `MCP_PAT_USER_PRINCIPAL_REQUIRED`; scheduled runs
  remain supported when their delegated principal is a user.
- Import metrics use only bounded scope, operation, and outcome labels; artifact
  and destination IDs stay in neither labels nor sanitized logs.
- Legacy workspace-scoped rows remain stored but dormant until an explicit ownership-mapped migration is available. New admin requests cannot create workspace-scoped rows.
- Connection responses expose only the installation ID, status, installation-derived
  auth type, and next action. Secret values and secret identifiers are not returned.
- Target calls resolve only the selected Cluster or VM registry tools. Agent calls resolve only the selected Agent installation. The built-in bridge is used only when the resolved tool is explicitly registered with source `builtin`.
- Generic remote servers use the configured URL as a single MCP Streamable HTTP
  endpoint. Each operation performs `initialize`,
  `notifications/initialized`, and the requested `tools/list` or `tools/call`,
  including negotiated protocol and server-issued session headers.
- Generic remote operations accept standard JSON or SSE responses and use an
  isolated, terminated-on-close session. REST-style appended `/tools/list` and
  `/tools/call` endpoints are not part of this contract.
- Manual MCP installation accepts only the actual absolute HTTPS Streamable HTTP endpoint. URL credentials and fragments are forbidden; non-secret query values may remain, but credentials belong in the authentication fields. Registry URLs, `server.json`, repositories, packages, containers, and stdio commands are not import mechanisms.

- Missing, malformed, or newly
discovered remote MCP tool capabilities default to `write` until an admin reviews and enables a narrower read classification.
- Remote MCP metadata is untrusted. Store discovered tools disabled, sanitize descriptions and schemas, and require capability review before enabling.
- Built-in control-plane bridge calls do not forward configurable public headers or secret-store auth headers.
- Public headers cannot override platform scope headers or credential headers.
- Public and auth headers cannot override MCP transport lifecycle headers.

## Change Checklist

When changing model streaming, MCP, JWT, native-tool, or admin surfaces:

1. Update handlers, models, settings, and manifests together.
2. Update mirrored counterpart manifests in execution-engine or control-plane.
3. Keep this README focused on durable boundary behavior only; do not paste endpoint, event, or field lists here.
4. Run `task contracts:check` and the workspace platform contract check when sibling repos are available.
