# LLM Gateway Core Beliefs

- Auth and policy boundaries matter more than provider-specific convenience.
- Normalize external variability at the adapter edge before it leaks into the rest of the system.
- Admin traffic and runtime traffic are separate contracts and should stay separate in docs and code.
- Durable broker behavior belongs in versioned docs and checks, not in prompt lore.
- Favor inspectable, testable transports and registries over opaque magic.
- Cross-repo contract changes must land with mirrored docs and checks.
