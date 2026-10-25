# Provider credit guidance

Status: completed, 2026-10-25. Gateway `.21` is verified in the demo.
Implementation branch: `fix/vm-telemetry-provider-errors`.

## Scope and design

The demo VM chat reached OpenAI, which reported no remaining API credits. The
gateway's conservative classifier previously fell back to `Provider request
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
- [x] Publish the gateway image after CI passes and update the demo image pin.
  Verify deployed image/readiness without paid model requests or key changes.

Companion telemetry repair: `control-plane/docs/exec-plans/completed/vm-telemetry-provider-errors.md`.

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
- Initial PR CI could not provision Python 3.12.11 on the new Ubuntu 26.04
  `ubuntu-latest` runner. The runner-only correction pins four Python jobs to
  supported Ubuntu 24.04, without changing the Python runtime or release jobs.
  All CI gates reran before merging. Dependency-security changes are recorded
  separately below.

## Release and live acceptance

- PR #10 merged as `817a1b0e9177e410f405da6ddbb784ef86fd594e`. Main CI
  40162272016 and release 40162701739 passed; tag `v0.0.1-experimental.21`
  publishes OCI index `sha256:d860f46a369667d1d578a57d791cf4357ab0bbb677731a65ff3aeb0b857ce362`.
- The separately approved dependency repair is recorded in
  [dependency-security-release.md](dependency-security-release.md). All original
  release gates passed without exclusions; unfixed base-OS findings remain tracked.
- Demo deployment 40162896202 passed. The live gateway image ID matches the
  published digest, all five services are ready, and public readiness checks pass.
  Persistent volumes, companion image pins, AgentV, credentials, and policy remain
  unchanged.
- A synthetic OpenAI SDK `APIError` was classified inside the deployed gateway:
  the exact actionable credit message was returned with `OPENAI_ERROR` and
  `retryable: false`. This constructed an exception only; no HTTP request, paid
  provider use, or key change occurred. Billing still requires administrator action.
- No authenticated demo-browser or paid end-to-end chat check was performed.
  Existing console formatter checks preserve the safe message; historical chat
  failures are not rewritten.
