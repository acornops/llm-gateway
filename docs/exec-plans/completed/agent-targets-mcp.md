# Agent Targets MCP

Remove the synthetic workspace/Agent-chat `targets` tool placement. Built-in
Targets tools are registered on each Agent as an ordinary Agent-scoped MCP
server and resolve through the existing exact server/tool identity path.

## Work

- [x] Remove synthetic `targets` argument rebinding and registry lookup.
- [x] Route built-in Targets calls through ordinary Agent MCP resolution.
- [x] Update affected tests and contract documentation.
- [x] Run gateway validation.
