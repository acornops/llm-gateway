# Readable model tool names

## Goal

Allow execution-engine to submit an authorized internal tool `name` plus an
optional readable `model_name` that provider adapters use for function
declarations.

## Constraints

- Preserve JWT validation against internal `name`.
- Leave MCP tool-call authorization and registry aliases unchanged.
- Treat `model_name` as optional so old and new services interoperate during a
  rolling deployment.
- Apply the existing provider-neutral function-name validation to both fields.

## Decisions

- Add `model_name` only to the normalized LLM tool specification.
- Provider adapters declare `model_name` when present and otherwise declare
  `name`, preserving legacy behavior.
- Tool calls returned by providers continue to contain the declared name; the
  execution engine owns translation back to the authorized internal alias.

## Validation

- `task validate`: 554 unit/contract tests and 50 keyless evaluations passed.
- Coverage verifies all four provider surfaces, internal-name JWT authorization,
  duplicate and reserved-name rejection, legacy omission, and deterministic dev
  tool calls.

## Completion

- All provider adapters expose the optional readable name consistently.
- Internal alias authorization remains unchanged.
- Requests without `model_name` retain existing naming behavior.
