# Agent chat identity cleanup

Status: completed 2026-08-01

## Goal

Authorize direct `agent_chat` requests by run, workspace, session, principal,
and Agent identity.

## Outcome

- Direct Agent-chat JWT, LLM, and MCP tool-call contracts require Agent ID and
  reject Workflow identity fields.
- Workflow specialist requests and tokens use Agent ID plus run-bound scope.

## Validation

- Canonical validation passed: lint, contracts, harness, 546 tests, and 50
  keyless evaluations.
