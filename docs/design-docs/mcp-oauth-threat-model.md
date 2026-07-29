# Automatic MCP OAuth Threat Model

## Scope and assets

This model covers individual-user authorization-code flows for remote HTTP MCP
installations. The protected assets are access and refresh tokens, authorization
codes, PKCE verifiers, state, user and workspace binding, public-client identity,
validated OAuth endpoint snapshots, and the authority to invoke MCP tools.

The browser, MCP server, authorization server, control plane, gateway, Redis,
gateway database, encrypted secret backend, DNS, and outbound network are
separate trust boundaries. MCP and authorization-server metadata are untrusted.

## Security invariants

- OAuth is available only to an authenticated AcornOps user and an installation
  configured for individual credentials.
- The client is always public. No client secret, initial-access token, static
  provider registration, confidential DCR result, or registration-management
  credential is accepted or stored.
- CIMD is selected when advertised. A CIMD failure is surfaced and never causes
  an implicit DCR downgrade.
- Every discovered URL is independently checked by the MCP egress policy.
  Production requires HTTPS, DNS pinning, preserved Host/SNI, no redirects,
  identity response encoding, bounded timeouts, and bounded bodies.
- Discovery requires protected-resource matching and exact issuer equality.
  The authorization server must advertise PKCE S256, authorization-code and
  code-response support. CIMD must advertise public-client token
  authentication; for DCR, the registration response is authoritative and
  must create a secret-free client using `token_endpoint_auth_method=none`.
  Authorization, exchange, and refresh all carry the same RFC 8707 resource.
- Flow state is random, encrypted at rest, short-lived, atomically consumed, and
  bound to workspace, installation, user, browser, issuer, resource, public
  client, callback, metadata fingerprint, and PKCE verifier.
- Tokens are opaque and stored only in a versioned encrypted per-user secret.
  Each bundle carries a cryptographic fingerprint of its public client,
  resource, issuer, and endpoint snapshot; use, refresh, and revocation fail
  closed unless that fingerprint matches the separately persisted connection
  metadata.
  Tokens, codes, state, verifier, authorization URLs, and provider responses are
  excluded from logs, metrics, audit metadata, and status APIs.

## Threats and mitigations

| Threat | Mitigation | Residual risk |
| --- | --- | --- |
| Authorization-code interception | Exact canonical callback, S256 PKCE, single-use state, authenticated callback session, browser binding | A fully compromised initiating browser can act as that user |
| CSRF and login CSRF | Cryptographically random state, `SameSite=Lax` HttpOnly binding cookie, exact user/browser/workspace/installation match, atomic consume | Authorization server account-selection UX remains provider-controlled |
| Authorization-server mix-up | Exact advertised/metadata issuer equality, explicit selection for multiple issuers, flow-pinned endpoints, RFC 9207 `iss` equality when returned, and rejection of a missing `iss` when the server advertised it | Older servers that do not advertise or return RFC 9207 `iss` rely on the already pinned issuer, client registration, PKCE, and state |
| SSRF and DNS rebinding | URL-form rejection, existing private-network policy, fresh validation and DNS pinning for every endpoint request, preserved Host/SNI | Explicitly allowlisted private hosts remain deployment trust decisions |
| Malicious metadata and redirect manipulation | No redirects, exact issuer and resource checks, response/schema bounds, canonical callback from trusted configuration, endpoint snapshots | A legitimately trusted issuer can still present malicious consent content |
| Confidential-client coercion | DCR sends `token_endpoint_auth_method=none`; responses containing secrets, changed redirects, or non-public auth methods are rejected | Some providers support only confidential clients and remain incompatible |
| Token theft | Encrypted secret backend, no browser token storage, no response exposure, per-user keying, token-to-endpoint binding, and no provider-controlled response bodies in logs; a newly issued or rotated bundle is best-effort revoked when secure persistence fails | Gateway or secret-backend compromise can expose active tokens |
| Refresh races and rotation loss | One connection-scoped local lock plus a transaction-scoped PostgreSQL advisory lock in production, pinned endpoint, persist rotation before lock release, no tool-call replay | A transport failure after remote rotation is ambiguous, so reauthorization is required |
| Disconnect versus in-flight authorization | Prepare, start, callback exchange, verification, refresh, and disconnect share the same connection mutation lock; Redis records and connection indexes are created atomically | A remote provider may retain an unused public DCR application because v1 intentionally omits registration deletion |
| Cross-user capability confusion | Every connection stores its own verified tool-name set. Existing shared tool schemas and reviewed read authority cannot be widened by a different user's authenticated discovery; conflicts fail verification | Provider-wide tool-definition changes require explicit installation review |
| Scope escalation | Challenge scopes take precedence; otherwise protected-resource scopes; `offline_access` only if advertised and disclosed; insufficient scope requires explicit reauthorization | A malicious resource may request excessive scopes, which remain visible for consent |
| Replay after authentication failure | Callback state uses atomic consume; runtime never retries a tool call after OAuth refresh or upstream authentication failure | Users must manually retry safe operations after repairing authorization |
| Secret leakage through telemetry | Bounded metric labels and stable error classes; authorization data omitted from logs and status APIs | Operators must preserve the same rules in downstream log processors |
| Open redirect on callback | Return path must be an absolute local path, rejects `//`, backslashes and control characters, and is stored inside encrypted flow state | Console routing changes must retain the local-path validation invariant |

## Failure behavior

Provider 429 and 5xx refresh responses are transient and do not delete valid
stored tokens. `invalid_grant`, an expired access token without refresh
capability, a confirmed MCP authentication failure, an ambiguous refresh
transport outcome, or a failure to persist a remotely rotated token transitions
the connection to `reauthorization_required`. Background work and tool
invocations never open a browser flow.

Disconnect and token-persistence failure handling attempt revocation only at
the pinned advertised endpoint. Remote revocation failure never prevents local
cleanup. If the local secret backend itself is unavailable, disconnect fails
closed and retains connection state so cleanup can be retried rather than
orphaning an undeleted token. DCR application deletion is intentionally
excluded.

## Verification

The security suite must cover discovery variants, path resources, bounded
multiple issuers, CIMD precedence, public-DCR response rejection, endpoint
egress and rebinding, advertised S256 PKCE, state/browser binding and replay,
RFC 9207 and RFC 8707, disconnect races, token rotation and ambiguous refreshes,
revocation cleanup, and telemetry leakage.
Real-provider canaries complement but do not replace deterministic mock-server
tests.
