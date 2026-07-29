# Automatic MCP OAuth

## Goal

Add provider-neutral individual OAuth for remote HTTP MCP installations using
Client ID Metadata Documents when advertised and unauthenticated public Dynamic
Client Registration as the fallback. Preserve existing none, bearer-token, and
custom-header behavior.

## Decisions

- OAuth installations always use individual ownership.
- CIMD is preferred and never silently downgraded after an advertised CIMD
  failure.
- DCR accepts public clients only (`token_endpoint_auth_method=none`); no client
  secrets or registration-management credentials are retained.
- Authorization state is short-lived and atomically consumed from Redis.
- Provider endpoints and resources are discovered, validated with the MCP egress
  policy, pinned, and never taken from browser input.
- Tokens are opaque encrypted secrets. Runtime refresh never replays a tool call.

## Work

- [x] Add OAuth discovery, registration, flow, token, and credential modules.
- [x] Add gateway-internal prepare/start/complete routes and publish CIMD
  metadata from the canonical control-plane URL.
- [x] Extend connection persistence, schema, readiness, runtime auth, metrics,
  revocation, and cleanup for OAuth lifecycle states.
- [x] Update contracts, operations/security documentation, threat model, and
  focused tests.
- [x] Add provider-neutral compatibility for opaque endpoint paths and
  unambiguous one-item resource arrays while requiring the authorization
  server to advertise the public-client and S256 capabilities needed by the
  flow.

## Validation

- Gateway OAuth, connection, configuration, and transport tests: 141 passed.
- Gateway full validation suite: 487 passed.
- Control-plane OAuth, configuration, and routing tests: 37 passed.
- Control-plane full suite against isolated PostgreSQL: 1,035 passed.
- Console OAuth-focused tests: 28 passed; console unit suite: 748 passed.
- Console MCP browser parity: 21 passed; design snapshots: 19 passed with
  1 intentional skip.
- Contract, OpenAPI, harness, lint, type, style, and diff checks: passed for the
  OAuth-owned surfaces.
- A live GitLab.com authorization completed through public DCR and encrypted
  token storage. The account's authenticated MCP endpoint returned not found
  during `tools/list`, which is retained as a verification failure rather than
  being misreported as an OAuth exchange failure.

## Remaining rollout evidence

An eligible GitLab 18.10 account and a public-DCR Keycloak environment are still
required for a complete non-production lifecycle. Authenticated `tools/list`,
token refresh, revocation, and reconnect remain rollout-canary evidence.

## Cross-repository dependency

Producer for control-plane OAuth routes. Merge before control-plane,
management-console, and deployment.
