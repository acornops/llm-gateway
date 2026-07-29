# Platform default LLM credentials

Status: complete

## Goal

Allow one write-only OpenAI, Anthropic, or Gemini credential to act as the
platform default for every workspace while preserving workspace-specific
overrides.

## Decisions

- Resolve provider credentials in this order: exact workspace scope, then
  platform-default scope.
- Never apply platform fallback to MCP or other secret families.
- Return only `configured`, `enabled`, and bounded source metadata; never return
  a provider key.
- Deleting a workspace override immediately restores the platform default when
  one exists.
- Fail closed on secret-backend errors instead of silently falling back.

## Validation

- Focused provider-admin and LLM stream tests.
- `task lint`, `task contracts:check`, `task harness:check`, and `task validate`.

## Outcome

- Workspace credentials take precedence over global provider defaults.
- Status responses identify `workspace`, `platform_default`, or `none`.
- Safe resolution logs include the credential source but no key material.
- `task validate` passed with 407 tests.
