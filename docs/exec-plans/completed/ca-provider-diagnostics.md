# CA trust and provider diagnostics

## Goal

Provide additive outbound CA trust and actionable, credential-safe provider
failure diagnostics with the smallest implementation that covers every LLM
Gateway outbound TLS dependency.

## Required runtime changes

- Replace the MCP-specific CA setting with `ADDITIONAL_CA_BUNDLE_FILE` and
  validate that the configured file is readable at startup.
- Extend each client's normal trust source rather than replacing it: HTTPX uses
  its normal trust context, while PostgreSQL and Redis retain their standard
  library trust behavior.
- Apply additional trust to providers, JWKS, Vault, remote MCP, `rediss://`, and
  explicitly TLS-enabled PostgreSQL, including migrations.
- Preserve internal mTLS as a separate trust boundary and do not turn plaintext
  Redis or PostgreSQL URLs into TLS connections.
- Pool one CA-enabled HTTP client per provider using that SDK's native timeout,
  redirect, and transport defaults, then close those clients at shutdown.
- Normalize supported PostgreSQL `ssl` and `sslmode` query parameters before
  SQLAlchemy passes them to asyncpg.
- Include provider, model, run/workspace correlation, sanitized effective
  endpoint, endpoint source, additional-CA state, bounded error category, and
  wrapped root cause on provider failures.
- Use only `LLM_PROVIDER_OPENAI_BASE_URL`,
  `LLM_PROVIDER_ANTHROPIC_BASE_URL`, and `LLM_PROVIDER_GEMINI_BASE_URL` as the
  AcornOps endpoint contract. Pass an explicit canonical/default URL to each
  SDK so its ambient base-URL variable cannot silently change routing.
- Combine the generic additional CA with the dedicated internal transport CA
  when both are configured, while keeping public roots out of that explicit
  internal trust boundary.

## Removed during review

- Duplicate route fields on request and stream-completion logs.
- Endpoint details on circuit-open logs, which do not make an outbound call.
- A new per-attempt metric that duplicated existing provider and dependency
  failure metrics.
- Debug traceback serialization; the bounded root-cause fields are sufficient
  for certificate, DNS, timeout, connection, and HTTP diagnosis.
- A startup route event that duplicated the endpoint fields present on every
  provider failure.
- A second execution-plan artifact for the same pending change.

## Validation

- Focused CA, provider diagnostics, adapter, and streaming tests: 86 passed.
- Installed-library semantic check: passed for OpenAI, Anthropic, Google GenAI,
  HTTPX, SQLAlchemy/asyncpg, and Redis client construction and shutdown.
- `task validate`: passed, including Ruff, contracts, harness checks, and 272
  unit tests.
- Loopback TLS handshakes accepted the configured CA and rejected a hostname
  mismatch for both generic and restricted internal contexts.
- `git diff --check`: passed.

## Residual risk

No live provider, PostgreSQL, or Redis endpoint was contacted. The review used
the installed production client libraries and exercised their construction,
configuration, exception wrapping, and lifecycle behavior without network
credentials.
