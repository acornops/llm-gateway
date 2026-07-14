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
- For Gemini, `web_search` accepts the supported native-tool surface and rejects unsupported domain-filter requests.
- The internal model-only skill tool is `_acornops_load_skill`; the manifest entry is `"internalModelOnlyTools": ["_acornops_load_skill"]`.
- `INTERNAL_MODEL_ONLY_TOOLS`, `is_reserved_internal_tool_name`, and `_validate_stream_tool_names` protect the reserved `_acornops_` namespace.
- Local smoke tests may set `LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES`, but production settings reject deterministic provider responses.

## Control-Plane Boundary Notes

- Config must keep `AUTH_ISSUER` and `AUTH_AUDIENCE` aligned with the control-plane run-token issuer and audience.
- `ADMIN_API_TOKEN` gates internal MCP and provider-credential administration.
- Workspace workflow built-in tool calls are forwarded to the control-plane built-in MCP bridge only after an enabled workspace registry entry identifies the tool as built-in.
- Workspace workflow scope uses `scope.type = "workspace"` and explicit workflow identifiers; ordinary workflow selection does not imply an agent id.
- The built-in MCP bridge is `acornops-cluster-agent` at `http://control-plane:8081/internal/v1/mcp`.
- Built-in bridge calls use `Authorization: Bearer <run-scoped-jwt>`, scope source `run-scoped-jwt-claims`, and call path `POST /internal/v1/mcp/tools/call`.
- Optional `tool_call_id` values are forwarded as `toolCallId` only on this
  trusted built-in bridge, preserving AgentK idempotency while keeping generic
  MCP request bodies unchanged.

## Generic MCP Boundary Notes

- Registry records have an explicit `scope_type` of `workspace` or `target`. Scope-omitting internal requests remain target-scoped for compatibility.
- Admin responses expose only whether a credential is configured. Secret values and secret identifiers are not returned.
- Workspace workflow calls resolve enabled workspace-scoped registry tools. The built-in bridge is used only when the resolved tool is explicitly registered with source `builtin`.

- Missing, malformed, or newly
discovered remote MCP tool capabilities default to `write` until an admin reviews and enables a narrower read classification.
- Remote MCP metadata is untrusted. Store discovered tools disabled, sanitize descriptions and schemas, and require capability review before enabling.
- Built-in control-plane bridge calls do not forward configurable public headers or secret-store auth headers.
- Public headers cannot override platform scope headers or credential headers.

## Change Checklist

When changing model streaming, MCP, JWT, native-tool, or admin surfaces:

1. Update handlers, models, settings, and manifests together.
2. Update mirrored counterpart manifests in execution-engine or control-plane.
3. Keep this README focused on durable boundary behavior only; do not paste endpoint, event, or field lists here.
4. Run `task contracts:check` and the workspace platform contract check when sibling repos are available.
