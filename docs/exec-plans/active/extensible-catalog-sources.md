# Extensible catalog sources

## Goal

Add a generic, workspace-owned catalog registry in the gateway, implement the
official MCP Registry v0.1 adapter, persist normalized artifact snapshots, and
import pinned remote MCP endpoints into exactly one workspace Agent or target
default-Agent scope.

## Boundaries

- Catalog data and Agent installation data remain separate.
- Catalog credentials are write-only and secret-backed.
- Imported server DTOs expose immutable provenance, review state, and target
  constraints, never credentials.
- Agent imports carry an Agent ID and optional structured target constraints.
  Target imports carry the server-derived target ID and target type and reject
  Agent constraints.
- Existing built-in Kubernetes MCP behavior remains unchanged.
- Target-scoped gateway records remain owned by the selected Cluster or VM
  generic agent, including existing third-party MCP servers. Agent-scoped
  imports coexist with them. Legacy workspace rows remain dormant until an
  explicit ownership-mapped migration is available.
- Runtime remote tools require an exact server-qualified ref and the acting
  user or service identity. Name-only registry lookup is forbidden.
- Uploaded adapters, arbitrary mappings, package-only, and stdio-only entries
  are unsupported.

## Validation

- Adapter pagination, incremental sync, version resolution, endpoint
  compatibility, digest, and provenance tests.
- Transactional/idempotent import, reimport, optimistic concurrency, Agent and
  target isolation, duplicate tool-name identity, destination-bound principal
  connection, bounded import metrics, and permission-mode tests.
- `task validate` and platform contract checks.
