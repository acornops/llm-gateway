# LLM Gateway Quality Score

Assessment date: May 28, 2026.

| Area | Score | Evidence | Main Gap |
| --- | --- | --- | --- |
| Execution-engine contract alignment | 5/5 | Mirrored contract docs, manifests, repo checks, reusable NDJSON replay fixtures for stream normalization | Keep replay fixtures current as provider stream behavior evolves |
| Control-plane admin integration | 4/5 | Admin APIs, scope fields, builtin bridge docs and checks, admin-token regression tests at API boundary | Broaden live integration coverage beyond mocked transports |
| Auth and policy enforcement | 4/5 | JWT and service-token checks, JWKS readiness, scope mismatch handling, issuer/audience drift regression tests | Expand negative-path coverage for token expiry and key-rotation failure modes |
| MCP broker behavior | 4/5 | Registry and transport docs, compatibility fallbacks, malformed-response regression coverage | More end-to-end coverage against live third-party MCP servers would help |
| Image and dependency readiness | 4/5 | Non-root gateway image, pinned constraints, `.dockerignore`, SBOM/provenance workflow, vulnerability scan workflow | Supply-chain workflow should be validated in CI |
| Harness knowledge base | 4/5 | AGENTS entry point, indexed docs tree, plan directories, quality/security/reliability docs | Freshness still depends on docs being updated with behavior changes |
