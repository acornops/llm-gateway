# MCP pending-change polish

## Goal and constraints

Review pending MCP changes across the workspace without committing, pushing,
discarding unrelated changes, or expanding product behavior. Consolidate the
duplicated terminal credential teardown through its existing helper. Preserve
owner locks, absent-user fast paths, revocation, both secret identities, and
row retention after external cleanup failure.

## Plan

- [x] Inspect pending changes, contracts, deployment instructions, and scope.
- [x] Establish baseline: gateway validation and 713 unit tests pass.
- [x] Reuse the existing owner cleanup helper for terminal deletion paths.
- [x] Run gateway validation and record cross-repository validation results.

## Decisions

Leave AgentV architecture documentation and local UI audit images untouched.
Do not remove lifecycle fencing or migration safeguards merely to reduce diff
size. A missing connection row needs no redundant database delete; its secrets
and flows still require cleanup during server deletion.

## Validation

Reviewed on 2026-09-05. Commands run from their respective repository roots:

- Gateway: `task validate` passed after the refactor (713 unit tests and 52
  keyless evaluations); `task lint`, `task harness:check`, and `git diff --check`
  passed after the final comment cleanup.
- Management Console: `VITE_APP_DATA_MODE=control-plane npm run validate`
  passed its 1,070 tests, accessibility/style/translation/fixture/contract
  gates, production build, and bundle budget. The final preview step could not
  bind inside the sandbox; rerunning
  `VITE_APP_DATA_MODE=control-plane npm run smoke:routes` with local port access
  passed.
- Platform Admin Console: `npm run validate` passed with local port access
  (91 tests, build, contracts, and smoke routes).
- Deployment: `task validate` passed, including local fixture profiles and
  production chart/image/edge checks.
- Public docs: `npm run validate` passed with Mintlify cache access;
  `npm run links` found no broken links.
- Workspace: `node scripts/harness/check-platform-contracts.mjs` and
  `node scripts/harness/check-runtime-truth.mjs` passed.
- Control plane: `npm run db:migrate` followed by `npm run validate` passed with
  `DATABASE_URL` and `CONTROL_PLANE_TEST_DATABASE_URL` targeting a disposable
  PostgreSQL 16 database named `acornops_mcp_test`: 1,209 tests passed, none
  failed or skipped, followed by all contract gates and the build. The test
  database container was removed after validation. The initial sandboxed run
  lacked the required isolated database configuration and local network access
  and is not acceptance evidence.

## Scope and residual risks

Most pending changes concern workspace registry removal, seed-only defaults,
direct registration, endpoint/credential consistency, lifecycle fencing,
OAuth, permissions, tool pagination, and their mirrored contracts and tests.
The deployment startup grace and development seed fix are the prior local-up
repair. Workspace AgentV architecture edits and six local UI audit screenshots
are separate work and were preserved.

No new public contract or operational behavior changes were introduced by this
refactor. Existing operations documentation remains valid. The broader pending
release still requires a coordinated maintenance migration and individual-user
credential reset; workspace-owned credentials are preserved. Live external
OAuth-provider, Vault, and full gateway integration environments were not
reprovisioned in this review. No production-readiness guarantee is implied by
the automated checks.

No commits or pushes were made. Reviewed child repository HEADs match locally
cached `origin/main`; remote refs were not fetched. Gateway and execution-engine
remain on `fix/reliable-document-tool-calls` without a configured upstream.
