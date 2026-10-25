# Provider credit guidance

Status: in progress, 2026-10-25.
Branch: `fix/vm-telemetry-provider-errors`.

## Scope and design

The demo VM chat reached OpenAI, which reported no remaining API credits. The
gateway's conservative classifier currently falls back to `Provider request
failed`. Recognize documented structured credit/billing/quota codes (including
SDK streaming APIError bodies), never arbitrary message substrings.

Keep the existing provider error code and payload shape, with bounded actionable
copy and `retryable: false`. The console already displays these safe messages;
no console release, new UI component, translation system, or error-code contract
is needed. Unknown errors remain generic, and genuine rate limits retain their
current behavior. Use the classified result to avoid gateway-level retries and
circuit penalties for OpenAI billing failures on both supported API surfaces.
Do not change SDK retry configuration, credentials, models, or other providers.

## Checks

- [x] Add failing classifier tests for HTTP and stream errors, credit vs broader
  quota limits, misleading text, unknown codes, and ordinary rate limits.
- [x] Add failing OpenAI adapter regressions proving terminal billing guidance
  without gateway retries/circuit penalties for Responses and Chat Completions.
- [x] Implement the minimal classifier/adapter changes and run targeted tests.
- [x] Run `task validate`; verify the existing console formatter presents the
  new guidance without replacing it with retry/model-switch advice.
- [x] Update the nearest operations guidance; review with the telemetry change.
- [ ] Publish the gateway image after CI passes and update the demo image pin.
  Verify deployed image/readiness without paid model requests or key changes.

Companion telemetry repair: `control-plane/docs/exec-plans/active/vm-telemetry-provider-errors.md`.

## Validation evidence

- Classifier regression: 8 expected failures before the fix.
- Adapter regression: 4 expected failures before the fix (extra HTTP retries or
  generic streaming errors). Both API surfaces now return actionable safe copy.
- `.venv/bin/python -m pytest test/test_provider_errors.py test/test_openai_adapter.py test/test_openai_chat_completions_adapter.py -q`: 71 passed.
- `task validate`: lint, contracts, harness, 735 unit tests, and 52 keyless
  evaluation cases passed. Initial test line-length lint failures were corrected.
- Existing console formatter exercised with both exact messages: preserved
  unchanged. Three existing chat/VM telemetry test files: 29 passed.
- Workspace `node scripts/harness/check-platform-contracts.mjs`: passed.
- Independent read-only review found no blocking or minor findings; independently
  reran 43 gateway tests. SDK-internal retries, other providers, and historical
  backfill remain unchanged by design. No live provider call or key change made.
