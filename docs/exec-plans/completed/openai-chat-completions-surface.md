# OpenAI Chat Completions API surface

Status: completed 2026-07-23

## Goal

Support OpenAI Chat Completions as an explicit outbound compatibility surface
without changing the normalized AcornOps request or NDJSON stream contracts.
Keep the Responses API as the default and retain the existing OpenAI credential,
endpoint, TLS, retry, circuit-breaker, logging, and error boundaries.

## Constraints

- Select the OpenAI API surface explicitly at deployment time; do not infer or
  automatically fall back between endpoints.
- Keep API-surface selection deployment-wide, matching the existing
  deployment-wide OpenAI base URL.
- Preserve custom function calling, streaming text, usage accounting, and
  reasoning-effort requests where Chat Completions supports them.
- Fail before the provider request when Chat Completions is selected with an
  AcornOps native tool, whose normalized contract remains on Responses.
- Report reasoning summaries as unavailable because Chat Completions does not
  expose the Responses reasoning-summary event stream.
- Do not add an inbound `/v1/chat/completions` compatibility route.
- Provider credentials, prompts, tool arguments, and raw model output must not
  enter logs or client-facing provider errors.

## Implementation

1. Add `LLM_PROVIDER_OPENAI_API_SURFACE` with validated values `responses` and
   `chat_completions`, defaulting to `responses`.
2. Keep `OpenAIAdapter` as the registry entry and route internally to dedicated
   Responses and Chat Completions stream implementations.
3. Translate Chat Completions messages, custom function tools, output-token
   limits, reasoning effort, content deltas, fragmented tool calls, and usage
   into the existing normalized contracts.
4. Add the selected surface to safe OpenAI provider-failure diagnostics.
5. Add request validation for native tools that require the Responses API.
6. Add replay-style and focused tests for configuration, routing, text,
   parallel tool calls, usage, capability degradation, parameter retries,
   provider failures, and no-retry-after-output behavior.
7. Update operator documentation and the deployment repository's Compose and
   Helm configuration surfaces.

## Cross-repository impact

- Producer/runtime: `llm-gateway`
- Deployment consumer: `acornops-deployment`
- Shared branch: `feat/openai-chat-completions-surface`
- Merge order: `llm-gateway`, then `acornops-deployment`
- No control-plane or execution-engine contract change is expected because the
  normalized gateway request and stream event contracts remain unchanged.

## Validation

- Focused OpenAI adapter, stream-handler, settings, and diagnostics suite:
  100 tests passed after the production-readiness cleanup.
- The focused suite includes replay fixtures for text, usage, reasoning tokens,
  and parallel fragmented tool calls plus an installed-SDK test using an HTTPX
  mock transport to exercise the real `/v1/chat/completions` SSE parser.
- The exact production lock, including OpenAI SDK 2.37.0, passed `pip check`,
  Ruff, contract checks, harness checks, and all 392 unit tests.
- Deployment `task validate`: passed, including Compose rendering, Helm schema
  and rendering, release-matrix, image, edge, and contract checks.
- Deployment `task platform-contracts`: passed.
- Workspace `node scripts/harness/check-platform-contracts.mjs`: passed.

## Decisions made during implementation

- Keep Responses as the default. Selection is explicit and deployment-wide;
  the adapter never guesses or falls back between API surfaces.
- Reuse the normalized inbound request and NDJSON event contracts. This is an
  outbound provider-route change, not an inbound OpenAI-compatible API.
- Require the current Chat Completions contract: `max_completion_tokens` for
  output limits and `stream_options.include_usage` for usage accounting. Do not
  silently downgrade to deprecated or partial endpoint behavior.
- Reject the AcornOps native-tool contract before secret lookup when Chat
  Completions is selected.
- Fail closed when either OpenAI surface returns malformed or non-object
  function arguments, and when Chat Completions returns an incomplete tool
  call.

## Production-readiness cleanup

- Removed fallback to deprecated `max_tokens`.
- Removed fallback that discarded `stream_options.include_usage` and could
  silently report zero usage.
- Removed synthetic tool-call IDs and silent omission of incomplete tool calls.
- Added installed-SDK SSE coverage for fragmented function-call deltas.
- Emit `reasoning_summary_unavailable` for summary requests because Chat
  Completions does not expose the Responses reasoning-summary stream.

## Rollout and rollback

Merge `llm-gateway` before `acornops-deployment`. Roll out with the default
`responses` value first, then explicitly set `chat_completions` only for
deployments that require that provider surface. Roll back by restoring
`LLM_PROVIDER_OPENAI_API_SURFACE=responses`; no data migration is involved.

## Completion criteria

- Both configured surfaces produce the same normalized text, tool-call, final,
  and error event shapes for their supported capabilities.
- Invalid surface values fail settings validation.
- Chat Completions never silently drops native tools or reasoning-summary
  requests.
- Failure diagnostics identify the configured OpenAI surface without leaking
  request or credential data.
- Compose, Helm values, Helm schema, and operational docs expose the setting
  with `responses` as the default.
- Required checks pass or any environment-blocked checks are recorded with an
  exact reason and residual risk.
