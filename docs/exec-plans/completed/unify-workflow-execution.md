# Unified Workflow execution claims

## Goal

Make Workflow execution the only workspace run identity accepted by the LLM
gateway while preserving the separate target-run contract and Agent-scoped MCP
capability registry.

## Changes

- Replace `workflow_run_id` with `execution_id` in claims and requests.
- Require `executor_role` for Workflow runs.
- Forbid Agent identity on coordinator runs.
- Require matching Agent ID and version on specialist runs.
- Reject Agent-without-Workflow request and token shapes.
- Keep target-run identity and Agent-owned MCP installations unchanged.

## Validation

- `task validate`: passed; 394 tests passed with Python, contract, harness, and
  Ruff checks.
- Cross-repository integration Compose and workspace contract checks: passed.
