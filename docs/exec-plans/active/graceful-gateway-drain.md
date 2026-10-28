# Gateway graceful rolling restart plan

Branch: `feat/graceful-rolling-restarts`. Coordinated producer/consumer plan lives
in the deployment repository's 2026-10-29 graceful rolling restart task briefs.

1. Add full-ASGI admission and first-signal process drain with bounded cleanup.
2. Add bounded authority retries and builtin pre-admission retries while preserving
   approval receipt claims, logical tool identity, operation fencing and tool policy.
3. Verify stream finalizers and authority cleanup with real SIGTERM subprocesses,
   including TLS, repeated signals and cancellation-resistant work.
4. Mirror additive producer contracts, update operations documentation, run full
   validation and hand off the scoped diff for independent review.

No migrations, new service, agent protocol change or crash resumption is included.
