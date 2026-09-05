# Production MCP Lifecycle Hardening

## Goal

Make internal MCP installation lifecycle changes safe under concurrent gateway
replicas. Lifecycle teardown must remain service-token-only and idempotent, may
remove platform built-ins only when a whole destination or workspace is being
deleted, and must not leave credential secrets or OAuth state behind. Trust
changes, built-in reconciliation, and OAuth operations must observe the same
durable lifecycle fence.

## Constraints

- Preserve ordinary generic CRUD behavior: callers still cannot delete or
  redefine a built-in server.
- Keep manual MCP endpoint URLs immutable and preserve existing external
  request and response shapes unless this plan explicitly adds an internal
  lifecycle route.
- Use PostgreSQL-backed coordination in production so independent gateway
  replicas cannot race. Tests and local SQLite must retain deterministic
  behavior without requiring PostgreSQL advisory locks.
- Never retain plaintext credentials in database lifecycle metadata or logs.
- Preserve the current uncommitted endpoint-invariant and built-in-sync work in
  this checkout.
- Change only `llm-gateway`; downstream control-plane adoption is a separately
  coordinated consumer change.

## Decisions

- Model a lifecycle fence as durable, scoped database state with monotonically
  increasing epochs. Server connection mutations must bind to the observed
  epoch and recheck it while holding the relevant database lock before secret
  persistence or OAuth token mutation.
- Destination and workspace teardown acquire broader fences before enumerating
  child servers. The teardown APIs are idempotent and may delete built-ins;
  ordinary per-server deletion remains immutable for built-ins.
- Cleanup performs a final fenced sweep so a connection that began before the
  fence cannot become a surviving secret-backed installation.
- Built-in synchronization rejects fenced destinations and updates the server,
  tool set, and stale-tool removal in one registry transaction. Existing user
  server/tool enablement is read at commit time rather than trusted from a
  stale synchronization payload.
- OAuth start and completion recheck both operator feature flags before any
  outbound request or token/connection mutation.
- Public unauthenticated direct installation checks the remote-MCP kill switch
  before creating the server row.
- Public headers may not collide case-insensitively with a custom credential
  header.
- Individual credentials are bound to a positive Control Plane membership
  generation. The pinned-pair rollout performs an explicit offline reset of
  legacy individual rows, secrets, and OAuth state before activating the new
  owner lifecycle.
- Runtime authorization re-reads current server/tool authority under the
  server lock but releases database coordination before remote MCP I/O.
- Catalog and MCP terminal cleanup use bounded, inventory-backed secret
  identities and durable transition cursors so retries cannot lose ownership
  of crash-created credentials.

## Work

- [x] Add durable lifecycle-fence schema, migration, store API, and scoped
  teardown routes.
- [x] Integrate fence/epoch checks into connection, OAuth, trust-change, server
  deletion, and built-in synchronization paths.
- [x] Make built-in reconciliation atomic and preserve user enablement at
  commit time.
- [x] Enforce OAuth feature flags, preflight remote create, and reject header
  collisions.
- [x] Add generation-bound individual credential lifecycle, offline reset,
  retry-safe secret/OAuth cleanup, and rollout preflight coverage.
- [x] Fence retained catalog producers and make catalog source/import/sync
  authority and generated-credential transitions retry-safe.
- [x] Add regression and concurrency tests for every changed invariant.
- [x] Update internal contract and operations documentation.
- [x] Run focused and repository-required validation.

## Validation Log

- `task python:check` passed on Python 3.12.11.
- `task contracts:check` passed, including executable lifecycle/built-in
  sources and mirrored endpoint vectors.
- `task harness:check` passed after focused helper extraction kept every
  production Python module within the 650-line repository budget.
- `task lint` passed repository-wide Ruff validation.
- `task unit-test` passed: 713 tests.
- `task validate` passed: Ruff, contracts, harness, 713 unit tests, and 52/52
  keyless provider/transcript evaluations.
- Focused MCP lifecycle, OAuth, connection, runtime, catalog, migration
  preflight, advisory-lock, and secret-reset regressions passed: 273 tests on
  the pre-closeout focused run; final added disconnect/bootstrap cases are
  included in the 713-test repository run.
- `MCP_MIGRATION_TEST_DATABASE_URL=postgresql+asyncpg://... \
  .venv/bin/python -m pytest -q test/integration/test_mcp_migrations_postgres.py`
  passed: 1 test covering populated PostgreSQL a100 -> b200 -> c300 upgrade.
- Environment-configured `pytest test/integration/` passed: 4 Docker-backed
  integration tests using real PostgreSQL lifecycle locks and mock MCP/auth.
- `git diff --check` passed after final code and documentation changes.

## Completion Criteria

- Repeated destination/workspace teardown returns success and leaves no server,
  tool, connection, OAuth registration/flow, or credential-secret state in
  scope, including built-ins.
- Concurrent connect, verify, OAuth start/complete, trust change, built-in sync,
  and teardown cannot cross a lifecycle epoch and commit stale credential state.
- Ordinary built-in deletion remains rejected.
- Remote/OAuth feature flags stop their respective OAuth stages before outbound
  or token mutation.
- Built-in sync cannot revert a concurrent user enablement change and cannot
  expose a partially replaced tool catalog.
- Focused concurrency/regression tests and the repository validation entrypoints
  pass, with any environment-limited checks recorded here and in handoff.
