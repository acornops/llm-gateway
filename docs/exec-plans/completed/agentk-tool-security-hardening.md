# AgentK tool security hardening

Carry the execution-engine tool call identifier through the built-in MCP bridge
so AgentK can derive a stable operation ID for deterministic write retries.

## Scope

- Add the optional tool call ID to the gateway request contract.
- Forward it only to the trusted built-in control-plane MCP bridge.
- Keep third-party MCP request bodies unchanged.
- Add focused contract tests and run the repository validation suite.

## Result

`tool_call_id` is now accepted from execution-engine and forwarded only to the
trusted built-in control-plane bridge as `toolCallId`. External MCP request
bodies remain unchanged.

## Validation

- `task validate`: passed; 204 tests plus contract and harness checks.
- Workspace validation: passed.

## Production review finding

The execution engine already supplies a model tool call ID, but the gateway
dropped it before invoking the control plane. The control plane consequently
generated a fresh JSON-RPC ID for every attempt, defeating AgentK write
idempotency after an ambiguous timeout.
