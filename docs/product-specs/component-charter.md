# LLM Gateway Component Charter

## Responsibilities

- Validate runtime run tokens and admin tokens.
- Normalize provider requests and responses.
- Stream normalized NDJSON output to execution-engine.
- Broker MCP tool calls and registry-managed remote servers.

## Non-Goals

- Browser-facing operator UX
- Long-term source of truth for cluster lifecycle state
- Silent escalation of permissions beyond token/config scope

## Primary Consumers

- Execution-engine
- Control plane
