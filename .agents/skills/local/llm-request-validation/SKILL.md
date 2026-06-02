---
name: acornops-llm-request-validation
description: Validate llm-gateway request safety, auth scope enforcement, provider routing, and MCP tool-call policy behavior. Use when changing request schemas, auth logic, adapter selection, tool broker behavior, or secret backend integrations.
---

# Inputs

- changed handlers, auth modules, adapters, and MCP registry logic
- expected JWT claims and scope rules
- provider/model policy expectations

# Procedure

1. Validate issuer, audience, and scope checks for each affected path.
2. Verify provider/model routing constraints and defaults.
3. Confirm MCP tool-call authorization and argument validation.
4. Ensure errors and logs do not leak secrets or sensitive payloads.
5. Run gateway test suite for changed paths.

# Outputs

- request policy validation report
- backward-compatibility and policy-change summary
- required remediation actions
