# LLM-Gateway Contracts

This repo sits between execution-engine and tool/model providers. Its direct platform contracts are with the control plane and execution-engine.
Machine-readable contract data for this repo lives in `docs/contracts/manifest.json` and is checked alongside this document.

## Full Platform Matrix

- Management console -> control plane
- Control plane <-> execution-engine
- Control plane <-> llm-gateway
- Control plane <-> k8s-agent
- Control plane <-> vm-agent
- Execution-engine -> llm-gateway

## Platform Dependency Summary

| Counterpart | Direction | Contract surface |
| --- | --- | --- |
| Execution engine | execution-engine -> llm-gateway | LLM streaming API and MCP tool-call API |
| Control plane | control-plane -> llm-gateway | Internal MCP admin API and workspace AI provider credential admin API |
| Control plane | llm-gateway -> control-plane | JWKS fetch for run JWT validation and builtin MCP bridge |

## Shared Invariants

- Runtime run permissions come from the control-plane-signed JWT. llm-gateway must enforce them, not infer them.
- Admin traffic and runtime traffic are separate contracts with separate credentials.
- The builtin target-agent bridge is just another MCP server from llm-gateway's perspective, but the control-plane-owned URL and auth semantics are contractually important.
- Internal service-to-service transport is HTTP by default and HTTPS/mTLS when the Kubernetes Helm chart sets `internalTransport.tls.enabled=true`. mTLS is transport hardening only; admin tokens and run-scoped JWTs remain required.
- If the request or response shape here changes, update this file and the mirrored execution-engine or control-plane contract doc in the same change.

## Execution-Engine Contract

Transport may be plaintext HTTP by default or HTTPS/mTLS when enabled by Helm
`internalTransport.tls`. The run-scoped JWT remains required in both modes.

### Streaming inference API

Execution-engine calls:

- `POST /api/v1/llm/generations:stream`

Required auth:

- `Authorization: Bearer <run-scoped-jwt>`

Request body:

- `run_id`
- `workspace_id`
- optional `scope.type`
- `target_id`
- `target_type`
- optional `workflow_id`
- optional `workflow_run_id`
- optional `workflow_session_id`
- optional `workflow_step_id`
- optional `agent_id`
- optional `agent_version`
- optional `trigger_id`
- `session_id`
- `provider`
- `model`
- `messages`
- `temperature`
- optional `max_output_tokens`
- optional `reasoning.{summary_mode,effort}`
- optional `tools[]`
- optional `native_tools[]` for built-in runtime tools. v1 supports `web_search` with optional `config.domainFilters.allowedDomains` and `config.domainFilters.blockedDomains`.

Execution-engine may include the internal model-only pseudo-tool `_acornops_load_skill`
in `tools[]` so supported providers can request frozen target skill context. The
gateway forwards this tool spec to the model but excludes it from
`permissions.allowed_tools` enforcement because it is intercepted inside
execution-engine and is never executable through MCP, approvals, public tool
previews, or normal tool-call audit. Tool names beginning with `_acornops_` are
reserved for AcornOps internal pseudo-tools. Streaming requests accept only the
documented model-only names from that reserved namespace and reject any other
`_acornops_` tool name before provider credentials are loaded. Reserved names
must not be registered as MCP tools.

Response media type:

- `application/x-ndjson`

Response event shapes:

- `{"type":"delta","text":...}`
- `{"type":"tool_call","call_id":...,"tool":...,"arguments":{...}}`
- `{"type":"reasoning_summary_delta","text":...,"provider":...}`
- `{"type":"reasoning_summary_completed","text":...,"provider":...}`
- `{"type":"reasoning_summary_unavailable","provider":...,"reason":...}`
- `{"type":"final","usage":{"input_tokens":...,"output_tokens":...,"tool_calls":...,"reasoning_tokens":...}}`
- `{"type":"error","code":...,"message":...,"retryable":...}`

Reasoning summary events contain provider-generated summaries only. The gateway must not emit raw chain-of-thought, encrypted reasoning items, thinking signatures, provider credentials, or provider-internal reasoning state.

Local development may set `LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES=true` to make the gateway emit deterministic NDJSON events for smoke tests after all JWT scope, provider/model, tool-allowlist, and native-tool allowlist checks pass. This mode is rejected in production settings and is not a production provider contract.

### Tool-call API

Execution-engine calls:

- `POST /api/v1/mcp/tool-call`

Required auth:

- `Authorization: Bearer <run-scoped-jwt>`

Request body:

- `run_id`
- `workspace_id`
- optional `scope.type`
- `target_id`
- `target_type`
- optional `workflow_id`
- optional `workflow_run_id`
- optional `workflow_session_id`
- optional `workflow_step_id`
- optional `agent_id`
- optional `agent_version`
- optional `trigger_id`
- `tool`
- `arguments`

Response body:

- `result`
- `is_error`

The tool-call API rejects internal model-only pseudo-tools such as
`_acornops_load_skill` even when the run token has wildcard tool permission. Those
pseudo-tools are only valid in streaming requests and are intercepted by
execution-engine before MCP execution. The broader `_acornops_` prefix is also
reserved and rejected for executable tool calls.

### Runtime auth expectations

The JWT must validate against control-plane JWKS and include:

- `iss`
- `aud`
- `run_id`
- `workspace_id`
- `target_id`
- `target_type`
- optional `scope.type = "workspace"`
- optional `workflow_id`
- optional `workflow_run_id`
- optional `workflow_session_id`
- optional `workflow_step_id`
- optional `agent_id`
- optional `agent_version`
- optional `trigger_id`
- `session_id`
- `permissions.allowed_providers`
- `permissions.allowed_models`
- `permissions.allowed_tools`
- `permissions.allowed_native_tools`
- optional `permissions.allowed_tool_operations` mapping tool names to `read` or `write` for downstream audit classification. This field is accepted but does not alter gateway authorization.
- `permissions.max_output_tokens`

This repo must reject requests whose body scope does not match token scope.
It must reject requested executable tools that are missing from `permissions.allowed_tools`.
It must also reject any requested built-in native tool that is missing from `permissions.allowed_native_tools`, or whose requested config does not exactly match the run-scoped claim.
For Gemini, `web_search` rejects non-empty `allowedDomains` and currently rejects domain filtering when blocked domains are requested, because the supported request surface does not expose equivalent domain filter controls.

Workspace workflow runs continue to rely on the control-plane-signed JWT. Workflow tokens add workflow scope fields such as `scope.type = "workspace"`, `workflow_id`, `workflow_run_id`, `workflow_session_id`, current step id, optional direct/delegated agent `agent_id`, `agent_version`, `trigger_id`, allowed tools, allowed tool operations, and context grants. Ordinary orchestrated workflow runs may omit `agent_id`; the gateway must not derive it from workflow-selected agents. The gateway enforces token claims and must not infer workflow MCP access from management-console UI state.
For `POST /api/v1/mcp/tool-call`, target-scoped runs must include `target_id` and `target_type`; workspace workflow runs must include `scope.type = "workspace"` plus the workflow identifiers and may omit target fields unless the workflow step is explicitly target-bound. Workspace workflow built-in tool calls are forwarded to the control-plane built-in MCP bridge with the original run-scoped JWT, without consulting target MCP registry state.

## Control-Plane Contract

Transport may be plaintext HTTP by default or HTTPS/mTLS when enabled by Helm
`internalTransport.tls`. The admin token, JWKS validation, and run-scoped JWTs
remain required in both modes.

### Control plane -> llm-gateway admin API

Control plane manages workspace AI provider credential status and target tool/MCP registry state through:

- `GET /api/v1/internal/llm/provider-credentials?workspace_id=<workspaceId>`
- `PUT /api/v1/internal/llm/provider-credentials/{provider}`
- `DELETE /api/v1/internal/llm/provider-credentials/{provider}?workspace_id=<workspaceId>`
- `GET /api/v1/internal/mcp/servers`
- `GET /api/v1/internal/mcp/tools`
- `PATCH /api/v1/internal/mcp/tools/{tool_name}`
- `POST /api/v1/internal/mcp/servers`
- `PATCH /api/v1/internal/mcp/servers/{server_id}`
- `POST /api/v1/internal/mcp/servers/{server_id}/test`
- `DELETE /api/v1/internal/mcp/servers/{server_id}`

Admin auth:

- `Authorization: Bearer <ADMIN_API_TOKEN>`

Provider credential status/write/delete scope is workspace-only. LLM provider
secret names are `{provider}_api_key` and tenant scope is
`{"workspace_id":"<workspaceId>"}`. Target-scoped secrets are reserved for
MCP/server credentials. Provider credential status responses include only
`provider`, `configured`, and `enabled`; they must not expose key values,
ciphertexts, or secret names.

Target scope is required for MCP registry endpoints and supplied via
`workspace_id`, `target_id`, and `target_type` query/body fields. Supported
target types are `kubernetes` and `virtual_machine`.
For an existing target-scoped tool, `target_type` is immutable. A caller that supplies the same `workspace_id`, `target_id`, and tool name with a different `target_type` must receive a conflict instead of silently moving the tool between target types.

Fields control plane depends on preserving in server/tool payloads:

- server: `id`, `workspace_id`, `target_id`, `target_type`, `server_name`, `server_url`, `enabled`, `auth_type`, `auth_secret_name`, `auth_header_name`, `auth_header_prefix`, `public_headers`, `connection_status`, `last_discovery_at`, `last_discovery_error`, `tools`
- tool: `name`, `mcp_server_url`, `timeout_ms`, `description`, `capability`, `version`, `source`, `input_schema`, `enabled`

Tool `capability` values are `read` or `write`. Missing, malformed, or newly
discovered remote MCP tool capabilities default to `write` until an admin reviews
and enables a narrower read classification.

### llm-gateway -> control-plane JWKS

This repo validates runtime JWTs against:

- `GET /api/v1/auth/jwks.json`

The control plane must keep issuer and audience aligned with gateway config:

- `GATEWAY_TOKEN_ISSUER` -> `AUTH_ISSUER`
- `GATEWAY_TOKEN_AUDIENCE` -> `AUTH_AUDIENCE`

### llm-gateway -> control-plane builtin MCP bridge

When control plane registers builtin Kubernetes or VM tools, it configures an MCP server with:

- `server_name = acornops-cluster-agent`
- `server_url = http://control-plane:8081/internal/v1/mcp`
- auth type `none` in the MCP registry

llm-gateway then reaches the control plane via:

- `POST /internal/v1/mcp/tools/call`
- `Authorization: Bearer <run-scoped-jwt>`

This builtin bridge is the only internal HTTP exception to remote MCP egress
validation. Registration must use the configured builtin server name and URL,
auth type `none`, no public headers, and tools marked with `source: builtin`.

For builtin tools, scope source is `run-scoped-jwt-claims`: workspace, target, run, session, and allowed-tool scope come from the run-scoped JWT. llm-gateway must not forward configurable MCP `public_headers`, MCP secret-store auth headers, or caller-supplied platform scope headers to the builtin bridge.

Request body:

- `name`
- `arguments`

Response body must remain MCP-style:

- `content: [{ type: "text", text: string }]`
- `isError: boolean`

## Generic MCP Server Contract

Although not one of the five platform repos, this contract is owned here and matters to control-plane-managed integrations.

### Discovery order

llm-gateway discovers tools in this order:

1. `POST {server_url}/tools/list`
2. `GET {server_url}/tools/list`
3. JSON-RPC fallback: `POST {server_url}` with method `tools/list`

Newly discovered remote MCP tools are stored disabled for admin review. Their
remote descriptions and JSON schemas are treated as untrusted metadata and
sanitized before storage. Disabled discovered tools are visible through admin
server/tool catalog responses but are excluded from runtime tool lists and LLM
tool specs until explicitly enabled with a reviewed capability. Discovery cannot
infer audit safety from remote metadata; discovered tools therefore default to
`write` capability while disabled.

### Tool execution order

llm-gateway executes tools in this order:

1. `POST {server_url}/tools/call`
2. JSON-RPC fallback: `POST {server_url}` with method `tools/call`

### Headers forwarded to MCP servers

Depending on context, the gateway may forward:

- `x-workspace-id`
- `x-target-id`
- `x-target-type`
- `x-run-id`
- configured non-secret `public_headers`
- configured auth header derived from secret store

Remote servers should treat these headers as authoritative scope metadata when present.
Public headers are applied before platform scope and auth headers; they cannot override platform headers or carry credential-like names.
Builtin control-plane bridge calls are excluded from this generic forwarding behavior and use only the run-scoped JWT authorization header.

Tool responses may arrive either as a direct MCP-style payload or as a JSON-RPC 2.0 envelope with a `result` field. Both shapes are part of the supported compatibility contract.
