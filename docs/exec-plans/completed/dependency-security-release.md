# Dependency security release plan

Status: completed, 2026-10-25. User approved the bounded dependency-security
work needed to release the VM telemetry and provider-credit fixes.

Goal: clear the gateway's existing security gates without unrelated upgrades,
major-version migrations, API changes, or audit suppressions.

Architecture: retain the gateway's existing runtime and dependency tooling.
Update only reported vulnerable packages and tightly coupled dependencies;
regenerate the hash-locked runtime requirements. Keep current feature checkouts
as requested. Execute inline with one independent final dependency review.

## Scope and evidence

The pre-change CI audit reported 23 vulnerabilities in anyio 4.13.0, certifi
2026.4.22, urllib3 2.7.0, mcp 1.28.1, opentelemetry-api 1.41.1, and pyjwt 2.13.0.
All were already on main. Preserve the existing image security gate and validate
the full runtime plus development environment, not just direct requirements.

Files: `requirements.txt`, `pyproject.toml`, `constraints.txt`, generated
`requirements.lock`, `deployments/Dockerfile.gateway`, and this evidence record.
Update nearby development guidance for reproducible dependency maintenance.
Do not change auth policy,
provider SDKs, MCP contracts, database schema, or application logic unless a
specific compatibility regression proves it necessary.

## Task 1: Bounded dependency repair

- [x] Reproduce the failing lockfile audit and verify upstream fixed versions.
- [x] Select bounded updates: anyio 4.14.2, certifi 2026.10.19, urllib3 2.8.0,
  MCP 1.30.0, PyJWT 2.15.1, and OpenTelemetry 1.45.1 / instrumentation 0.66b1.
  Confirm package metadata and resolver compatibility before installation.
- [x] Regenerate hashes with `pip-compile`, upgrading only selected packages;
  compare every changed locked version with the allowlist above.
- [x] Install the hash-locked runtime and constrained development dependencies;
  run `pip check`, lockfile/environment `pip-audit`, and `task validate`.
- [x] Review JWT rejection behavior, MCP transports, TLS trust behavior, and
  OpenTelemetry instrumentation through existing unit/integration tests. Run
  platform contract checks and obtain a focused independent review.
- [x] Build and scan the final image, including its separately bundled `pip`;
  retain existing security gates with no new exclusions.

Expected: `pip check`, both `pip-audit` checks, and `task validate` exit zero;
all changed locked versions are from the selected security-update set.

## Task 2: Release and demo verification

- [x] Commit the dependency repair separately and rerun all PR CI gates. Merge
  both service fixes only after green checks, then require successful main CI
  for the exact commits tagged for release.
- [x] Publish the two service images, update only their demo image pins, validate
  demo-infra, and perform the normal non-destructive demo rollout.
- [x] Verify live VM samples, app readiness, deployed versions, and unchanged
  persistent-volume identities. Archive plans with evidence after completion.

## Review focus

- Reject expired, wrong-issuer/audience, malformed, and unsupported JWT tokens;
  do not relax verification to accommodate a dependency change.
- Keep MCP 1.x client transport/auth behavior and TLS verification intact.
- Keep OpenTelemetry's API, SDK, semantic conventions, and instrumentation
  mutually compatible; preserve existing metrics/instrumentation behavior.
- Do not let lock regeneration upgrade unrelated provider libraries.
- Never use a live paid provider request, replace keys, reinstall AgentV, or
  reset demo/local databases to validate these changes.

## Upstream references

- [MCP advisory](https://github.com/modelcontextprotocol/python-sdk/security/advisories/GHSA-84m7-p3x7-pcfv)
- [OpenTelemetry advisory](https://github.com/open-telemetry/opentelemetry-python/security/advisories/GHSA-xcq7-p3pf-p88h)
- [JWT options advisory](https://github.com/jpadilla/pyjwt/security/advisories/GHSA-gvp8-978c-rx2q)
- [PyJWT 2.15.1](https://github.com/jpadilla/pyjwt/releases/tag/2.15.1)

## Results and decisions

The security audit is the red/green regression check for these
dependency/configuration changes; existing behavior tests guard compatibility.
Baseline audit reproduced all 23 findings. User approved continuing this work
after the previous release stopped at the security gate.

- Locked versions changed only for the selected 12 packages. OpenTelemetry no
  longer depends on `importlib-metadata`/`zipp`, so the generated lock drops them.
- `pip-audit -r requirements.lock`: zero known vulnerabilities after the update.
- `pip check`: passed. The local full-environment audit initially found old
  audit/packaging tools (not app dependencies); refreshing those tools, as CI
  already does, produced a clean full-environment audit without manifest changes.
- `task validate`: 735 unit tests and 52 keyless evaluations passed, plus lint,
  contracts, and harness. Workspace platform contract check passed.
- Real gateway JWT validation accepted a valid RS256 token and rejected seven
  invalid cases: expiry, issuer, audience, algorithm, malformed token, signature,
  and future not-before. PyJWT's caller-options mutation reproduced on 2.13.0
  and was absent on 2.15.1. No live key or provider request was used.
- Independent dependency review found no critical, important, or minor issues;
  requested real JWT checks are recorded above. Image/integration and deployment
  evidence remain executor-owned release gates.
- Image inspection found bundled `pip` 25.0.1 outside the application lock.
  Upgrading to 26.2.2 alone was insufficient because its vendored dependencies
  remained vulnerable; the final design below removes unused runtime installers.
- PR CI 40161099271 passed lint, unit, integration, and dependency-audit checks,
  but its image scan found four high-severity entries in pip's vendored msgpack,
  setuptools, and urllib3. The application and OS packages had no actionable
  high/critical findings. Inspecting pip 26.2.2's `vendor.txt` confirmed the old
  bundled versions; independently upgrading app dependencies cannot replace them.
- No application, migration, or runtime entrypoint uses pip/ensurepip. Retain
  pinned pip only during the build, run `pip check` before uninstalling it, and
  remove both runtime pip installations plus the base ensurepip bootstrap. This
  removes unused installer code, not scanner evidence or exclusions. A runtime
  absence assertion failed before this change; rebuilt-image smoke and CI scan
  guard the final design. Local/development pip remains available.
- Rebuilt image passed the installer-absence assertions for both interpreters,
  non-root application import, `alembic --version`, `alembic history`, and
  `uvicorn --version`. Build-time `pip check` passed. `task validate` passed
  again (735 tests and 52 keyless evaluations). The subsequent hosted image scan
  also passed, as recorded below.
- Final PR CI 40161882679 and main CI 40162272016 passed every required gate.
  PR #10 merged as `817a1b0e9177e410f405da6ddbb784ef86fd594e`; that exact main
  commit is tagged `v0.0.1-experimental.21`. The unchanged actionable image gate
  reports zero fixable high/critical findings. The full scan still lists 44
  high-severity base-OS entries with no fixed version, also present before the
  installer cleanup. Track these in the quality score and tech-debt record;
  do not describe the image as free of all known vulnerabilities.
- Release 40162701739 published gateway `.21` from the tagged commit; verified
  OCI index `sha256:d860f46a369667d1d578a57d791cf4357ab0bbb677731a65ff3aeb0b857ce362`.
  Control-plane `.36` was published after its own successful main CI. Demo
  commit `60a91ca` changes only those two image pins/defaults and records the
  rollout; infrastructure CI 40162896215 and local Ansible validation passed.
  Deployment 40162896202 completed successfully.
- Live acceptance passed: both image digests match publication, all five services
  are ready, all public readiness checks pass, and all three PVC identities are
  preserved. The affected VM recovered from zero samples to two fresh CPU/memory
  samples without reinstalling AgentV. New pod logs show no inventory duplicate
  errors. The deployed billing classifier returns the expected non-retryable
  guidance for a synthetic SDK exception without contacting a provider.
- The deployed gateway also passes installer-absence and non-root assertions.
  No reset, secret/key change, policy relaxation, paid request, or user-VM mutation
  was performed. Authenticated browser/paid chat checks remain outside this run;
  unfixed base-OS advisories remain an explicit follow-up, not a cleared finding.
