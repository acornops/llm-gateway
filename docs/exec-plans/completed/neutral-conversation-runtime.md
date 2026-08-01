# Neutral conversation runtime

Status: completed 2026-08-01

## Goal

Accept the explicit `agent_chat` run scope issued by the control plane without
weakening the existing target or Workflow (`workspace`) authorization rules.

## Boundaries

- Agent chat requires an exact Agent identity and forbids Workflow and target
  binding fields at the top-level scope.
- Dynamic target routes remain limited to the signed route allowlist.
- Existing target and Workflow request/token matching remains unchanged.

## Validation

- Agent-scope claim, LLM request, and tool request tests.
- Existing JWT, streaming, MCP tool, contract, lint, and unit suites.

## Outcome

- Added strict `agent_chat` JWT, LLM request, and tool-call validation with an
  exact Agent identity and no top-level target or Workflow binding.
- Preserved signed dynamic target routes for Agent tools and left existing
  target and workspace/Workflow scope matching unchanged.
- `task validate` passes, including lint, contracts, harness checks, 546 tests,
  and 50 keyless tests.
