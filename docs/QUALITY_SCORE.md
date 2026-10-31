# LLM Gateway Quality Score

Assessment date: July 31, 2026.

| Area | Score | Evidence | Main Gap |
| --- | --- | --- | --- |
| Execution-engine contract alignment | 5/5 | Mirrored contracts, exact four-surface renderers, sanitized SDK-locked stream replays, fail-closed 50-scenario keyless evaluator | Keep fixtures current as SDK/provider behavior evolves and retain credentialed canaries |
| Control-plane admin integration | 4/5 | Admin APIs, scope fields, builtin bridge docs and checks, admin-token regression tests at API boundary | Broaden live integration coverage beyond mocked transports |
| Auth and policy enforcement | 4/5 | JWT and service-token checks, JWKS readiness, scope mismatch handling, issuer/audience drift regression tests | Expand negative-path coverage for token expiry and key-rotation failure modes |
| MCP broker behavior | 4/5 | Registry and transport docs, compatibility fallbacks, malformed-response regression coverage | More end-to-end coverage against live third-party MCP servers would help |
| Image and dependency readiness | 4/5 | Non-root runtime without package installers, hash-locked dependencies, clean dependency audits, SBOM/provenance and image-security CI | Track unfixed base-OS advisories; see the tech-debt tracker |
| Harness knowledge base | 4/5 | AGENTS entry point, indexed docs tree, plan directories, quality/security/reliability docs | Freshness still depends on docs being updated with behavior changes |

## Hosted-readiness evidence — September 6, 2026

723 unit tests and 52 keyless evaluations passed. Independent scoped review exercised post-initialize MCP suspension, first-provider-dispatch lease loss and definite fallback operation identities.
The replica probe substitutes a deterministic operation body; it does not measure
live provider reliability or model outcome quality. Existing scores are unchanged.

## Focused image-security evidence — October 25, 2026

[CI 40161882679](https://github.com/acornops/llm-gateway/actions/runs/40161882679)
passed unit/integration tests, dependency audits, and the existing image-security
gate. The scan has zero fixable high/critical findings. Its full report retains
44 high-severity base-OS entries with no fixed version listed (unchanged from the
preceding scan); this is not a claim that the image has no known vulnerabilities.
No scanner exclusions or auth/TLS policy changes were introduced. Scores outside
image/dependency readiness were not reassessed by this focused repair.

## Scoped hardening evidence — October 31, 2026

[Validation and retention checks](exec-plans/completed/2026-10-31-validation-retention.md)
cover webhook bounds, PostgreSQL cleanup races, real-process schema deadlines,
cancellation and unknown-write outcomes. Fresh review findings have regression
coverage. Existing scores and broader integration coverage gaps remain unchanged.
