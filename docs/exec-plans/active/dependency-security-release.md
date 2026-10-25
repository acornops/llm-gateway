# Dependency security release plan

Status: in progress, 2026-10-25. User approved the bounded dependency-security
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
- [ ] Review JWT rejection behavior, MCP transports, TLS trust behavior, and
  OpenTelemetry instrumentation through existing unit/integration tests. Run
  platform contract checks and obtain a focused independent review.
- [ ] Build and scan the final image, including its separately bundled `pip`;
  retain existing security gates with no new exclusions.

Expected: `pip check`, both `pip-audit` checks, and `task validate` exit zero;
all changed locked versions are from the selected security-update set.

## Task 2: Release and demo verification

- [ ] Commit the dependency repair separately and rerun all PR CI gates. Merge
  both service fixes only after green checks, then require successful main CI
  for the exact commits tagged for release.
- [ ] Publish the two service images, update only their demo image pins, validate
  demo-infra, and perform the normal non-destructive demo rollout.
- [ ] Verify live VM samples, app readiness, deployed versions, and unchanged
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
- Image inspection found bundled `pip` 25.0.1; its separate audit reports 12
  entries (including duplicate aliases). Pin `pip` 26.2.2 in both shipped Python
  environments rather than changing the Python runtime or suppressing findings.
- `docker build -f deployments/Dockerfile.gateway -t acornops/llm-gateway:security-smoke .`:
  passed. A disposable container confirmed both pip installations are 26.2.2,
  `pip check` passes, the application imports, and the runtime user is non-root.
  The CI image scan and full integration suite remain mandatory before merge.
