# MCP release readiness

## Goal

Verify the pending MCP release without expanding its scope or changing the
running development stack. Exercise the real PostgreSQL integration and upgrade
paths omitted from the previous polish review, inspect release safeguards, and
provide an explicit release decision and behavior summary.

## Checklist

- [x] Read prior review evidence and inspect repository/contract state.
- [x] Run gateway validation and isolated MCP integration/migration tests.
- [x] Review maintenance/reset procedure and remaining deployment prerequisites.
- [x] Fix confirmed issues only, verify changes, and record the release decision.

## Constraints

Preserve unrelated dirty files. Do not commit, push, deploy, reset development
data, or run maintenance cleanup against existing workspaces. Test containers
use the dedicated `mcp-release-review` Compose project and are removed afterward.
Live production credentials, provider authorization, and Vault policies cannot
be certified from unit tests or local mock services.

## Evidence (2026-09-05)

- `task validate`: passed, including 713 unit tests and 52 keyless evaluations.
- `.venv/bin/python -m alembic upgrade head`: passed on dedicated PostgreSQL 16.
- `.venv/bin/python -m pytest test/integration/`: all 4 passed, none skipped.
  This includes real HTTPS MCP discovery/execution, Agent/target credential
  isolation, and the a100 → b200 → c300 PostgreSQL upgrade regression with
  unsafe-state blockers and lifecycle constraints.
- `.venv/bin/python -m app.scripts.mcp_user_lifecycle_preflight --fail-on-unbound`:
  passed on the cleaned integration database and real Redis with all counts
  zero. This verifies command execution, not production data cleanliness.
- Management Console `npm run smoke:mcp-parity`: all 8 passed.
- Management Console `npm run smoke:fixtures -- --grep 'MCP|mcp|tools'`:
  all 4 selected browser tests passed.
- Management Console `npm run smoke:fixtures -- --grep 'registry source management'`:
  the remaining workspace-settings browser check passed.
- Workspace `node scripts/harness/check-platform-contracts.mjs` and
  `node scripts/harness/check-runtime-truth.mjs`: passed.

Integration environment used the CI test values for `DATABASE_URL`,
`MCP_MIGRATION_TEST_DATABASE_URL`, `AUTH_JWKS_URL`, `AUTH_ISSUER`, `AUTH_AUDIENCE`,
and `SECRETS_KEK_BASE64`, with `SECRETS_BACKEND=database`,
`REDIS_URL=redis://localhost:6379/0`,
`INTEGRATION_MCP_URL=https://localhost:8002/mcp`,
`MCP_EGRESS_ALLOWED_HOSTS=localhost`, and
`ADDITIONAL_CA_BUNDLE_FILE=/private/tmp/mcp-release-review-certs/mock-mcp-cert.pem`.
The test database names were `gateway` and `gateway_mcp_migration_test`, owned
only by the `mcp-release-review` Compose project. A stalled Docker Hub build
was cancelled; cached fixture images ran the current bind-mounted test source.
The four test containers, dedicated network, and two disposable database/Redis
volumes were removed afterward. Development services and data were untouched.

Prior full control-plane, both console, deployment, and public-doc validation
remains recorded in [the preceding review](mcp-pending-change-polish.md).
Those entire suites were not needlessly repeated here; no production source was
changed in this pass. Full visual snapshot suites, image vulnerability scans,
and live provider/Vault checks were not performed in this local review.

## Release decision

No additional blocking MCP code defect was identified. Do not expand the diff
with speculative refactoring. The user confirmed this deployment is an upgrade;
unconditional production sign-off still requires the operations runbook to be
rehearsed against representative existing data and the actual secret backend.

Required cutover gates:

1. Commit/package the complete coordinated change set, including currently
   untracked implementation and migration files. Pin the matching control-plane
   and gateway images and require release CI/image security checks.
2. Notify users about individual-credential reset; close admission, drain runs
   and the old cleanup queue, stop all old writers, and back up both databases
   plus the secret namespace. Do not perform a rolling upgrade.
3. Run the endpoint preflight and approved cleanup, migrations, and explicit
   individual reset exactly as described in `docs/OPERATIONS.md`. Ordinary
   workspace-owned credentials survive the lifecycle reset, but endpoint-mismatch
   cleanup intentionally revokes credentials on affected installations.
4. Validate secret-backend permissions and reconciliation duration for actual
   workspace sizes. Vault additionally needs the documented maintenance LIST
   permissions; the readiness probe alone does not prove them.
5. Require zero migration/readiness blockers, preserved workspace-owned
   baselines, and actual provider smoke tests before reopening traffic. There
   is no application-only rollback; follow the documented backup/forward-fix
   boundary.

## Behavior summary

Workspace registry browsing, source management, import, and reimport disappear
from the Management Console; the catalog term and disabled registry placeholder
remain. Direct URL registration and platform-seeded starters converge on normal
workspace-owned installations without ongoing platform-admin update propagation.
Credential ownership, OAuth, tool permissions/readiness, paging, endpoint
validation, stale-credential fencing, and retry-safe secret cleanup are aligned
across the consoles, control plane, and gateway. Direct endpoint edits require
replacement; authentication configuration changes invalidate connections.
Dormant backend catalog storage/endpoints remain for future admin-console work.

## Final code/documentation coherence pass (2026-09-05)

No further runtime change was required. The public configuration overview and
reference still recommended registry bootstrapping and pointed to deleted
examples; those instructions were corrected. The same pass aligned installation
scope, snapshot-only defaults, enable-to-materialize behavior, immutable direct
endpoints, and credential invalidation with the implementation. Backend catalog
configuration remains documented as retained but does not enable the removed
Management Console workflow.

Fresh verification: gateway `task validate` passed (713 unit tests, 52
evaluations); control-plane
`NODE_ENV=test node --import tsx --test test/workspace-defaults-contracts.test.ts test/agent-mcp-contracts.test.ts test/services/mcp-user-lifecycle-worker.test.ts`
passed all 15 tests; both workspace contract/runtime-truth checks passed. Public
docs `npm run validate` and `npm run links` passed again after the final wording
cleanup. `task harness:check` and `git diff --check` also passed. The prior integration/browser evidence
above remains applicable because no runtime source was changed.
