# MCP Streamable HTTP Standards Support

Status: completed July 14, 2026.

## Goal

Replace the generic remote MCP transport's proprietary `/tools/list` and
`/tools/call` protocol with standards-compliant MCP Streamable HTTP while
preserving the separate trusted built-in Kubernetes bridge.

## Constraints

- Use the stable official MCP Python SDK and pin it below the unreleased v2
  line.
- Treat the configured remote server URL as the single MCP endpoint.
- Preserve production HTTPS enforcement, DNS/IP egress validation and pinning,
  bounded responses, timeouts, circuit breaking, secret-backed auth, and
  sanitized errors.
- Do not share remote MCP sessions across workspace, target, credential, or run
  boundaries.
- Do not route the built-in `acornops-cluster-agent` bridge through the generic
  remote MCP client.
- Remove the unreleased proprietary remote `/tools/list` and `/tools/call`
  compatibility behavior instead of carrying a legacy mode.

## Decisions

- Open one initialized Streamable HTTP session for each discovery or tool-call
  operation. This supports stateful servers without a cross-tenant session
  cache and ensures session termination on operation completion.
- Delegate MCP lifecycle, protocol negotiation, session headers, JSON/SSE
  parsing, and response correlation to the official SDK.
- Wrap the SDK HTTP client with AcornOps egress pinning, response ceilings,
  uncompressed-response enforcement, and bounded sanitized error telemetry.
- Expose an optional secret- or ConfigMap-backed additional CA bundle in the
  platform Helm chart so private-CA MCP endpoints can be configured without a
  custom gateway image. It extends normal trust only for generic remote MCP
  traffic; the existing exact-host allowlist and NetworkPolicy egress rule
  remain separate required controls.
- Retry idempotent discovery once only when the server explicitly reports that
  an attached MCP session was terminated. Never replay a tool call. Use the
  SDK's typed request API directly so it does not perform redundant discovery
  after a completed side effect; the gateway validates the result against its
  registered schema later in the request path.

## Validation

- Added focused remote transport tests for initialization, initialized
  notification, stateful sessions, JSON, SSE, pagination, concurrency
  isolation, session termination, stateless operation, supported and rejected
  negotiated protocol versions, header ownership, egress pinning, invalid-port
  handling, response ceilings, compressed-response rejection, sanitized errors,
  URL-credential/query redaction, circuit breaking, and no-replay tool-call
  semantics.
- Existing built-in bridge and tool-call regression tests remain unchanged and
  pass in the full suite.
- Converted the local Docker mock and seed URL to a stateless `/mcp` endpoint;
  its discovery/tool call smoke check and four integration/seed tests pass.
- `task lint`: passed.
- `task contracts:check`: passed after replacing the legacy fallback assertion
  with the Streamable HTTP lifecycle contract.
- `task harness:check`: passed.
- `task unit-test`: 244 passed.
- `pip check`: no broken requirements.
- `task validate`: passed with 244 tests.
- `node scripts/harness/check-platform-contracts.mjs`: passed from the workspace
  root.
- `task validate` in `acornops-deployment`: passed, including strict Helm lint,
  schema checks, private MCP CA bundle render checks, production edge checks,
  and production image metadata checks.
- Focused control-plane AgentK install-instruction tests: 7 passed; TypeScript
  typecheck and build passed.
- `pip-audit -r requirements.lock`: no known vulnerabilities.
- The production Docker image built successfully from `requirements.lock`,
  contains MCP Python SDK `1.28.1`, and discovered all three Microsoft Learn
  tools from inside the image with production egress enforcement enabled.

The real organization-private MCP endpoint, CA bundle, DNS, and cluster
NetworkPolicy are deployment-owned state and still require a staging/canary
connection test before a full production rollout. The complete control-plane
database-backed suite also requires its dedicated test Postgres URL and should
remain a required CI gate; the changed install-instruction path is covered by
the focused test above.

## Completion Criteria

- Strict stateful MCP Streamable HTTP servers can discover and call tools.
- Remote requests use the configured endpoint and standard lifecycle/headers.
- Generic remote traffic cannot enter the trusted built-in bridge path.
- Existing built-in Kubernetes tool behavior remains covered and unchanged.
- Durable architecture, operations, security, and contract docs describe the
  new boundary.
