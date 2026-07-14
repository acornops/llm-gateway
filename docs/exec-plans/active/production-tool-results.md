# Production Tool Results

Persist trusted output schemas and artifact policies, validate AgentK results, normalize model and complete views, and reject service skew explicitly. Completion requires migration, normalization, registry, contract, lint, and unit validation.

Implementation is complete, repository validation passes, and the strengthened Pod-only remediation gate passes 20 consecutive local model runs. Keep this plan active through the coordinated staging soak and production release gate.

Durable design: [Tool Result Normalization](/docs/design-docs/tool-result-normalization.md). The production review made missing schemas and mismatched trusted envelopes fail closed and applied the 2 MiB ceiling to third-party MCP responses.
