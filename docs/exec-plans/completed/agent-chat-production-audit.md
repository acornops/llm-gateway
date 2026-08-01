# Agent chat production audit

Status: completed 2026-08-01

## Outcome

- Confirmed Agent-chat JWT, LLM, and tool-call contracts require exact run,
  workspace, session, principal, and Agent identity while rejecting Agent
  version, Workflow identity, and top-level target binding.
- Confirmed target selection is accepted only through an exact signed target
  tool route and live registered tool authority.
- Preserved Workflow coordinator/specialist claim rules unchanged.

## Validation

- `task validate` passes with 546 tests.
- The keyless/provider safety harness passes 50/50.
