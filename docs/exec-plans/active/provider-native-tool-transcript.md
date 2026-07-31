# Provider-Native Tool Transcript

Implement the llm-gateway half of the coordinated breaking transcript contract
described by the workspace change set:

- `../change-sets/active/provider-native-tool-transcript-00-overview.md`
- Phase 1: strict Pydantic provider-neutral transcript models and sequence
  validation.
- Phase 2: replace loose `messages` with `runtime_instruction` plus
  `transcript`, and render exact native structures for OpenAI Responses, OpenAI
  Chat Completions, Anthropic Messages, and Gemini.
- Phase 3: regression fixtures for complete multi-step read/write/verify
  transcripts and provider-continuation round trips.

## Constraints

- Shared branch: `feat/provider-native-tool-transcript`.
- Intentionally breaking; no legacy `messages` parser, dual path, feature flag,
  or mixed-version compatibility.
- Provider SDK payloads and opaque continuation-state handling stay inside this
  repository.
- Provider state is bounded, namespaced, same-provider-only, excluded from
  ordinary logs and user-facing serialization, and never interpreted as
  reasoning or instructions.
- Existing auth, scope, provider/model/tool allowlists, retries, reasoning
  summaries, native-tool policy, and sanitized errors remain intact.

## Decision Log

- 2026-07-31: Started from clean `main` at
  `cc4bbe713c58eacdee8a8a6082897f68cad0f563`, equal to freshly fetched
  `origin/main`; created the shared feature branch with the workspace branch
  helper.
- 2026-07-31: The canonical HTTP contract uses a non-empty trusted
  `runtime_instruction` and a discriminated transcript. Provider adapters own
  all native message/item/content/part serialization.

## Validation Log

- Phase 1 (2026-07-31):
  `.venv/bin/python -m pytest test/test_transcript_contract.py -q` passed
  (10 tests); `task unit-test` passed (500 tests); `task validate` passed lint,
  contract, harness, and 500 tests.
- Phase 1 contract decisions: canonical turns are discriminated `user`,
  `assistant`, and grouped `tool_results`; call/result groups preserve exact
  ID/name/order; opaque state is same-provider-only, JSON-compatible, and
  bounded to 32 KiB.
- Phase 1 temporary bridge: `NormalizedLLMRequest.messages` remains the live
  field only until the Phase 2 breaking HTTP cutover.
- Phase 2 (2026-07-31): exact transcript/renderer fixtures passed (36);
  focused adapter tests passed (73); `task validate` passed lint, contracts,
  harness, and 526 tests. The legacy `messages` body is rejected.
- Phase 2 provider decisions: Responses replays required reasoning items and
  uses `function_call`/`function_call_output`; Chat uses assistant
  `tool_calls` plus `tool` messages; Anthropic uses `tool_use`/`tool_result`;
  Gemini uses `function_call`/`function_response`. Provider-issued IDs and
  bounded opaque reasoning/thinking/signature continuation metadata are
  round-tripped only to the issuing provider.
- Optional integration fixture: 1 passed and 1 was unavailable because the
  local PostgreSQL service was not running (`localhost:5432` refused). The
  deterministic Phase 2 gate is green; the required fresh-stack integration
  gate remains pending after Phase 3.
- Phase 3 deterministic gates (2026-07-31): the full canonical
  read/write/verify transcript is included in exact fixtures for all four
  provider surfaces; focused renderer/contract suite passed (40). A final
  provider-state audit added direct stream-normalization assertions for OpenAI
  reasoning items, Anthropic thinking/signatures, and Gemini provider call
  IDs/thought signatures, and made normalized continuation state use the
  bounded typed contract. The combined post-audit adapter/renderer/contract
  suite passed (87). Final `task validate` passed lint, contracts, harness, and
  530 tests.
- Workspace `task validate`, platform contracts, and platform harness passed.
- Optional integrated-flow rerun: 1 passed and 1 remained unavailable because
  the standalone test process expects PostgreSQL at `localhost:5432`, which the
  deployment Compose stack does not publish to the host.
- Final post-audit deployment gate (2026-07-31): reset completed; the gateway
  source image rebuilt from the shared feature branch; fresh migrations/import
  succeeded; and final status shows all 15 services running with the core
  services healthy. Default `local-smoke` reached provider dispatch and
  verified fail-closed `AI_PROVIDER_CREDENTIAL_MISSING`; the complete
  no-provider/fail-closed smoke then passed. Running-image inspection confirms
  `runtime_instruction` and `transcript` are present and legacy `messages` is
  absent.
- Conditional real OpenAI smoke: not run. No provider credential is configured
  in the final freshly reset workspace, and explicit approval to share the
  local remediation prompt/transcript externally was not granted. No external
  provider request was made; the fresh stack remains running.
- Post-completion keyless harness (2026-07-31): added a manifest-selected
  evaluator to `task validate`, plus sanitized Anthropic and Gemini stream
  replays beside the existing OpenAI Responses and Chat Completions replays.
  Fixture integrity is locked to `openai==2.37.0`, `anthropic==0.102.0`, and
  `google-genai==2.3.0`; provider credential variables are removed and a
  TCP-connect audit guard must activate. The evaluator fails closed for
  failures, skips, missing selectors, or unexpected collected nodes.
  `task keyless-eval` passed 50/50 cases: provider rendering 29, stream replay
  8, transcript contract 11, and fixture integrity 2. New focused replay and
  manifest checks passed 13/13. Final `task validate` passed lint, contracts,
  harness checks, all 539 unit tests, and the 50-case evaluator. These results
  measure deterministic provider mapping fidelity, not live-provider
  reliability or model answer quality.

## Completion Criteria

- Strict canonical validation rejects unknown fields, invalid sequencing,
  unsafe calls, and malformed or cross-provider continuation state before
  dispatch.
- All four provider surfaces render exact native structured calls/results and
  normalize stable call IDs plus required opaque continuation data.
- The old `messages` body fails validation.
- All required repository, workspace, and final fresh-stack gates pass.
