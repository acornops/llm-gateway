# Target-Bound Workflow Tool Routing

## Goal

Allow a workspace-scoped Workflow run with an exact target binding to resolve
and invoke that target's registered MCP tool after Agent-scoped resolution
misses.

## Constraints

- Preserve workspace-only Workflow behavior.
- Preserve exact tool references, target identity checks, reviewed authority,
  approval, and built-in bridge authentication.
- Support both specialist and coordinator Workflow executors.

## Outcome

- Workspace-scoped requests fall through to target tool resolution only when
  the signed run claims contain both `targetId` and `targetType`; the request
  must match those claims exactly.
- Target-bound specialist and coordinator routing is covered by regression
  tests.
- No request, token, registry, or control-plane contract changed.

## Validation

- `task validate`: 404 passed; lint, contracts, and harness checks passed.
- Cross-repository platform contract checks passed.
