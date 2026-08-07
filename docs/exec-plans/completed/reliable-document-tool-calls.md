# Reliable document tool calls

## Goal

Normalize safe OpenAI-compatible streamed tool-call variations without repairing
model content, and expose bounded metadata for one pre-execution correction.

## Completed changes

- Added a bounded Chat Completions accumulator for incremental fragments,
  repeated names, cumulative snapshots, and object-valued arguments.
- Added fail-closed identity and argument errors with safe tool metadata,
  retryability, and provider usage on Chat Completions and Responses.
- Added bounded-cardinality outcome counters, an argument-size histogram, and
  content-free structured diagnostics.
- Added replay fixtures and regression coverage for Markdown preservation,
  normalization variants, malformed JSON, non-object input, identity conflicts,
  and log redaction.
- Added bounded pre-stream compatibility fallback for explicitly rejected
  `stream_options` and `max_completion_tokens` request fields.
- Enforced one terminal event at the normalized gateway boundary so every
  consumer receives an explicit error for cleanly incomplete adapter streams.

## Validation

- `task validate` passed: 571 repository tests and 52 keyless evaluations,
  including ten provider stream-replay cases.

## Completed

Implemented and reviewed on 2026-08-07.
