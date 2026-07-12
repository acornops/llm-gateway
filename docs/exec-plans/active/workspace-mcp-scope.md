# Workspace MCP Scope

## Goal

Extend the existing secure MCP registry and broker from target-only records to explicit `workspace` and `target` scopes so workflow runs can execute agent-granted remote tools.

## Constraints

- Migrate every existing registry row to target scope.
- Preserve old target admin requests by defaulting omitted scope to `target`.
- Keep MCP credentials secret-backed and write-only.
- Preserve egress checks, tool allowlists, run-token scope checks, write approval claims, rate limits, sanitized logs, and tool latency/error metrics.
- Route workspace calls through enabled workspace registry entries; call the built-in bridge only for tools explicitly registered with source `builtin`.

## Validation Plan

- Migration and model checks for explicit scope and workspace isolation.
- Admin schema, discovery, secret auth, egress, tool enablement, and delete tests.
- Workspace remote execution and registered built-in fallback tests.
- `task validate` in the repository's pinned Python 3.12.11 environment.

## Completion Criteria

- Workspace and target records cannot collide or leak across scopes.
- Responses expose scope, discovery state, tool capability/enabled state, and credential configured state without secret values.
- Workspace workflow tool calls resolve enabled remote registry entries and retain governed failure telemetry.
