# Output Token Permission Enforcement Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Implementation authorized by the user on 2026-10-30 and complete on `fix/token-cap-and-agentv-defaults`; local validation passed. Independent review passed with no findings. Release and live provider canary remain separate.

**Reviewed baseline:** llm-gateway `9d4611e20b506da66990ca4f709c40a249bdb03a` (2026-10-30). Recheck source and working-tree changes before execution.

**Goal:** Ensure every provider request honors the signed run token's output-token ceiling even when the request omits that field or supplies null.

**Architecture:** Resolve the effective limit once at the authenticated HTTP boundary, after scope/provider/model checks and before deterministic-mode branching or provider dispatch. Pass a copied normalized request with that effective value through the existing adapters. Preserve rejection of explicit over-limit requests and preserve provider defaults when neither request nor claims supplies a cap.

**Tech Stack:** Python 3.12.11, FastAPI, Pydantic, pytest/pytest-asyncio, existing OpenAI/Anthropic/Gemini adapters.

**Spec:** The design/spec below is the self-contained specification for this plan. Existing authority requirements: [gateway contracts](../../contracts/README.md) and [security model](../../security-model.md).

## Global Constraints

- Signed JWT permissions remain authoritative; request bodies cannot remove or enlarge their ceiling.
- Preserve explicit over-limit HTTP 403 behavior and its current `max_output_tokens exceeds token permission limit` detail.
- Preserve optional request schema, positive request validation, scope validation, authentication, rate limits, execution authority, and streaming shape.
- Preserve provider defaults when both request and permission cap are absent/null.
- All four configured adapter surfaces must receive the same normalized policy: OpenAI Responses, OpenAI Chat Completions, Anthropic, and Gemini.
- Never turn provider failures into retries that omit the effective cap.
- Do not add monetary pricing, cumulative run-budget accounting, token counting, or a provider SDK upgrade.
- Invalid JWT claim types/ranges remain the authentication layer's concern; this change assumes a valid positive cap or None and must not silently treat invalid values as unlimited.
- Existing deterministic responses remain development-only and production settings must continue rejecting that mode.

## Review Focus

- Missing JSON property and explicit null have identical permission-enforcement semantics.
- A caller's smaller explicit cap must survive unchanged; equality must be accepted and a larger cap rejected.
- No permission cap must preserve today's omitted/default and explicit-limit behavior.
- Provider compatibility retries must retain the effective value while changing parameter names only when already supported.
- Target, Agent-chat, Workflow, native-tool, and deterministic routes must not introduce a bypass around the shared boundary.

## Evidence and affected files

`NormalizedLLMRequest.max_output_tokens` currently defaults to None. `stream_generation()` compares request and claim only when both are non-null. OpenAI and Gemini then omit their upstream cap, while Anthropic supplies its own default. An offline authenticated handler probe with permission 16 and an omitted request cap returned HTTP 200, passed None to a mocked adapter, and forwarded its 100-token usage as successful. This is a per-request authorization defect; it does not establish unbounded lifetime spend.

| File/function | Planned responsibility |
| --- | --- |
| `app/api/handlers_llm_stream.py`, `stream_generation()` | Apply policy before deterministic/provider branch to a request copy |
| `test/test_llm_stream.py` | Authenticated HTTP regression, scopes, side-effect order, development branch |
| `test/test_openai_adapter.py` | Responses SDK kwargs retain effective cap |
| `test/test_openai_chat_completions_adapter.py` | Current/legacy token parameter and retry retention |
| `test/test_anthropic_adapter.py` | SDK max_tokens receives effective cap instead of default |
| `test/test_gemini_adapter.py` | Generation config receives effective cap |
| `docs/contracts/README.md`, `docs/security-model.md` | Explain optional-field semantics and per-request scope |

Provider production files should not need changes: they already send non-null normalized limits. If regression tests expose an omission in a retry path, change only that adapter path and document it in review.

## Design/spec

### Effective limit decision table

Let `requested` be the validated optional body value and `permission_limit` be the valid optional signed permission.

| requested | permission_limit | Result |
| --- | --- | --- |
| omitted/None | 16 | Set effective cap to 16 |
| 8 | 16 | Keep 8 |
| 16 | 16 | Keep 16 |
| 17 | 16 | HTTP 403 before credentials, provider lookup/dispatch, or deterministic output |
| omitted/None | None | Keep None; existing provider defaults apply |
| 8 | None | Keep 8 |

Request values 0 and negative numbers must continue failing request validation with HTTP 422. Do not expand this patch into changing coercion behavior or JWT claim schema.

### Exact handler behavior

- Preserve the existing comparison and HTTP 403 detail for explicit over-limit requests.
- When the request cap is None and the permission cap is present, use `req = req.model_copy(update={"max_output_tokens": claims.permissions.max_output_tokens})`.
- Do not copy or alter requests with an explicit cap, and do not mutate the caller's model or claims.
- Keep normalization before `_validate_native_tools()`, `execution_authority.authorize()`, deterministic branching, credential lookup, and `adapter.stream()`.
- Adapter selection, tool/native-tool permissions, execution fencing, streaming, and earlier scope/provider/model rejection remain unchanged.

### Development responses and provider semantics

- `_deterministic_dev_events(req)` receives the same effective value as a real adapter; it cannot bypass the explicit-over-limit rejection.
- The deterministic generator uses synthetic usage and fixed text. This patch does not add a tokenizer or pretend its fixture usage enforces a real provider maximum; tests assert boundary normalization and rejection rather than synthetic token-count arithmetic.
- OpenAI Responses must send `max_output_tokens=effective_limit` when non-null.
- Chat Completions must send the existing selected `max_completion_tokens` or `max_tokens`, preserving the numeric cap on supported compatibility retry.
- Anthropic must send `max_tokens=effective_limit`, overriding its default only when an effective cap exists.
- Gemini must set `GenerateContentConfig.max_output_tokens=effective_limit` when non-null.
- Low caps may interact with provider thinking budgets or provider minimum requirements. Preserve existing adapter handling and surface a sanitized provider failure rather than silently increasing the cap.

### Compatibility and explicit exclusions

- No request/response field, token claim, route, or manifest version changes are proposed.
- Existing clients sending a valid explicit value behave identically. Clients omitting a cap while holding a capped token now receive the intended signed ceiling.
- Tokens without a ceiling retain existing defaults; this patch does not add a global default cap.
- This is a per-provider-request output bound. It is not a cumulative multi-turn token limit, input-context limit, or financial budget.
- No control-plane or execution-engine implementation change is needed. Their mirrored boundary prose should be checked if this behavior is documented in more than one place; do not invent a new claim.
- Invalid signed limits are not normalized to None. Any newly discovered JWT schema weakness should receive a separate auth finding/test plan, not be hidden by normalization.

## Implementation tasks

### Task 1: Pin the HTTP regression and decision table

**Interfaces:** Normalizes the optional limit at the existing authenticated handler boundary; no new helper or exception is needed.

- [x] Cover the HTTP decision table with cases `(None,16)->16`, `(8,16)->8`, `(16,16)->16`, `(17,16)->403`, `(None,None)->None`, and `(8,None)->8`.
- [x] Add authenticated route cases to `test/test_llm_stream.py` that truly delete the body key and separately set it to null; do not conflate omission with a builder's default 1000.
- [x] For each omitted/null case, sign/mock valid claims with cap 16, mock credentials and adapter, consume the ASGI response, and assert the adapter request has `max_output_tokens == 16`.
- [x] Preserve the current explicit-over-limit regression; assert HTTP 403, exact existing detail prefix, no secret lookup, no authority authorization, and no adapter/generator invocation.
- [x] Run the targeted new regressions and record their expected failures on the base code before implementation.
- [x] Add shared handler normalization at the existing cap-check position.
- [x] Assert the original request and claims objects are unchanged after direct handler invocation; only the adapter/generator copy carries the effective cap.
- [x] Run the stream tests; all decision-table and existing scope/permission tests must pass.

### Task 2: Prove adapter forwarding and retry retention

**Interfaces:** Consumes the normalized request from Task 1; provider SDK wire names remain adapter-owned.

- [x] Extend existing mocked SDK tests for OpenAI Responses, Chat Completions, Anthropic, and Gemini using cap 16; assert the precise upstream token parameter equals 16.
- [x] For Chat Completions, trigger the existing supported token-parameter compatibility fallback and assert both attempts retain 16 while only the parameter name changes.
- [x] Exercise existing temperature/reasoning compatibility retries where implemented; assert no attempt drops or enlarges the effective cap.
- [x] Add no-permission/no-request controls at the HTTP boundary, assert adapter receives None, and retain each adapter's existing default/omission assertions.
- [x] Add explicit below/equal/above request cases against permission 16; assert upstream values 8/16, and zero upstream calls for 17.
- [x] Exercise reasoning-enabled low-cap input with mocked provider behavior; assert no adapter raises the ceiling to accommodate reasoning.
- [x] Run the four adapter files plus the handler tests; change adapter code only if these assertions identify an actual forwarding defect.

### Task 3: Development branch and scope parity

**Interfaces:** Consumes the same effective request before provider/deterministic branching; no additional route-specific policy helper.

- [x] Parameterize relevant handler cases across target, Agent-chat, and Workflow fixtures with matching valid scope claims; assert omitted/null all resolve to 16.
- [x] Parameterize permission-cap behavior with deterministic mode enabled/disabled; explicit 17 must reject before output, while omitted/null pass a request copy capped at 16.
- [x] Spy on/mock `_deterministic_dev_events(req)` to inspect its effective value without coupling tests to synthetic fixed-text token counts.
- [x] Retain development-mode native/custom tool allowlist tests and production configuration rejection of deterministic responses.
- [x] Verify invalid request 0/-1 remains 422, invalid/mismatched tokens never dispatch, and no uncapped request escapes through a native-tool branch.
- [x] Run stream, JWT, configuration, and adapter tests together; verify mocked dependencies prevent external network calls.

### Task 4: Documentation and release preparation

- [x] Update gateway contract/security prose with the six decision-table outcomes, preserving per-request terminology and no-cap compatibility.
- [x] Check counterpart docs in control-plane/execution-engine for contradictory max-output semantics; update mirrored prose only where it already describes this behavior and run platform contract checks.
- [x] Run the full offline gateway validation commands below; collect one independent review of exception ordering, request-copy semantics, every adapter surface, and compatibility retries.
- [x] Prepare a focused local change on shared branch `fix/token-cap-and-agentv-defaults`; no provider SDK, manifest, migration, or counterpart implementation change.
- [ ] Before release, record the published candidate and live provider canary results; low-cap provider errors and rollback risks remain as described below.

## Validation matrix for implementation

Run from llm-gateway with the existing Python 3.12.11 environment and mocked provider SDKs:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider test/test_llm_stream.py -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider test/test_openai_adapter.py test/test_openai_chat_completions_adapter.py test/test_anthropic_adapter.py test/test_gemini_adapter.py -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -p no:cacheprovider test/test_jwt_validator_service.py test/test_config_settings.py -q
task validate
# From workspace, after any mirrored contract prose changes:
node scripts/harness/check-platform-contracts.mjs
```

`test/test_config_settings.py::test_production_settings_reject_deterministic_dev_responses` owns the production-mode rejection assertion. No provider credentials are needed for unit/replay validation. A separately authorized provider canary may verify actual low-cap behavior; it must not substitute for SDK-kwargs assertions.

## Rollout and rollback

One gateway release is sufficient; no database migration, run-token reissue, or coordinated engine rollout is expected. Canary an existing explicit-cap request and an omitted-cap request under the same capped signed permission. Verify upstream request metadata without recording JWTs, prompts, credentials, or completions. Watch sanitized provider validation errors for models with minimum output/reasoning requirements.

Rollback restores an authorization gap for omitted/null requests. Prefer a corrective gateway patch. If rollback is necessary, constrain affected callers to explicit valid caps through existing configuration while tracking the gateway defect as unresolved; caller compliance alone is not a replacement for boundary enforcement. Do not remove the 403 path or silently raise a permission cap to make a provider accept a request.

## Independent acceptance checklist

- [x] Recorded RED evidence reproduces the omitted/null bypass on the base revision; the independent reviewer verifies both cases now resolve to the signed ceiling.
- [x] Complete decision table passes at the authenticated route boundary without altering request/claims originals.
- [x] OpenAI Responses/Chat Completions, Anthropic, and Gemini send the effective numeric cap, including supported retry paths.
- [x] Explicit over-limit rejects before output or external side effects in general and deterministic modes.
- [x] No-permission defaults, valid explicit caps, request validation, scope auth, tool policy, and production development-mode rejection remain compatible.
- [x] Documentation distinguishes per-request output permission from cumulative token or financial budgets; full gateway checks and release evidence are attached.

## Effort and planning validation

Estimated engineering effort: **0.5–1.5 working days**, including roughly 1–2 hours helper/handler changes, 2–4 hours route/adapter/development regressions, and 1–3 hours full validation, independent review, and release preparation. Complexity is low-to-moderate; most work is proving identical enforcement across optional fields and SDK surfaces. Provider-specific minimum-token behavior may extend canary qualification but should not expand the patch.

Planning validation (2026-10-30): handler, normalized request, claim, deterministic branch, adapter, and existing regression code re-read; `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/check-harness.py` passed. No implementation code, runtime tests, or deployment has been changed by this plan. Runtime suites were not rerun for this documentation-only change; implementation commands above remain unchecked future work.


## Implementation evidence — 2026-10-30

- The production fix is three added lines in `stream_generation()`. Adapter production code, HTTP/token schemas, and manifest versions are unchanged.
- Baseline: stream suite **32 passed**. New omitted/null regressions failed in **13 cases** because downstream received None rather than 16. After normalization, stream suite **85 passed**.
- Expanded adapter/stream tests: **184 passed**, including caps 8/16, equality, no-cap defaults, native-tool rejection, low-cap reasoning, and supported temperature/reasoning/token-parameter retries. All three run scopes and both deterministic modes are covered.
- `task validate`: lint, contracts, harness, **855 unit tests**, and **52/52 keyless evaluations** passed. The initial sandbox run blocked 11 existing process-drain tests from binding localhost; the permitted loopback rerun passed. Four new-test line-length violations were corrected before the successful run.
- Workspace `task validate` and deployment `task platform-contracts` passed. Counterpart contract/security prose did not contradict these semantics and needed no edits.
- No live provider canary, release, deployment, or cumulative budget change was performed. A provider may reject a low cap; the gateway must preserve the signed ceiling.

### Implementation rulings

1. Keep normalization inline and cover the decision table through HTTP instead of adding a policy module, custom exception, and duplicate unit layer. The existing explicit rejection already works; future policy reuse could justify extraction.
2. Fold scope/development coverage into the same route decision table, then validate it with JWT/configuration/adapter suites. Parameter IDs keep combined failures identifiable.
3. Use dedicated branches in the current child checkouts after the app's earlier managed-worktree attempt rejected nested repositories. Explicit staging preserves unrelated plan edits; concurrent work in these checkouts remains a coordination risk.
4. Preserve the valid-positive-claim boundary and existing claim schema. Control-plane issuance uses positive validated configuration; malformed signed claims remain a separate authentication-hardening concern, not a new unlimited fallback.
5. Keep M2 deferred as explicitly requested. Source validation does not certify unpublished binaries or actual provider minimum-token behavior.


### Final review — 2026-10-30

A fresh reviewer found no Critical, Important, or Minor issues and judged the
source ready to merge. It independently ran the 184 adapter/stream tests, read
full validation evidence, and checked exception ordering, request-copy semantics,
all adapter surfaces, and compatibility retries. The combined JWT/configuration/
stream/adapter suite also passed 218 tests.

The executor accepted the review's scope rulings: malformed signed caps are a
separate pre-existing authentication concern (positive config issuance bounds
supported inputs); live provider minimums require a canary; cumulative budgets
and deterministic fixture token counts are separate policies. None justifies
raising or dropping a valid signed ceiling. M2 and prior high-priority fixes
remain outside this change. No deferred minor findings.
