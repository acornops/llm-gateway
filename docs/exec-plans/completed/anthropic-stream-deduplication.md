# Anthropic stream deduplication

## Goal

Emit each Anthropic text delta exactly once so a single provider response cannot
produce duplicated assistant text.

## Constraints

- Preserve the existing normalized gateway stream contract.
- Keep Anthropic tool calls, reasoning summaries, usage, retries, and circuit
  breaker behavior unchanged.
- Do not log response content.

## Decision log

- Treat the SDK's raw `content_block_delta` text event as canonical.
- Ignore the SDK's derived `text` convenience event, which repeats the same
  payload immediately after the raw event.
- Record normalized text-delta count and character count on the existing
  `llm_stream_completed` log for content-free per-run diagnostics.

## Validation

- Focused Anthropic adapter and gateway stream tests: 36 passed.
- The production-pinned Anthropic 0.102.0 wheel hash matched
  `requirements.lock`; its event builder produced raw-plus-derived text pairs,
  and the adapter normalized each pair to one delta.
- An in-process gateway request built from those SDK events returned
  `Hello world` once and logged two deltas totaling 11 characters.
- `task validate` passed against Anthropic 0.102.0, including Python version,
  Ruff, contracts, harness checks, and 395 unit tests.
- `git diff --check`: passed.

## Completion criteria

- A realistic raw-plus-convenience Anthropic event sequence emits one normalized
  text delta.
- Provider adapter and gateway stream tests pass.
- Repository validation passes.

## Residual risk

No live Anthropic endpoint was contacted. The regression and in-process gateway
request use the paired raw and derived events produced by the production-pinned
Anthropic SDK.
