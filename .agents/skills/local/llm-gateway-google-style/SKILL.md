---
name: acornops-llm-gateway-google-style
description: Apply Google Python Style Guide conventions to llm-gateway service code and keep FastAPI modules readable, maintainable, and policy-safe. Use when editing API handlers, auth, adapters, MCP broker code, or data access logic.
---

# Inputs

- changed Python files under `app/`
- auth and policy constraints
- runtime and test commands

# Procedure

1. Apply Google Python style conventions for naming, doc clarity, and function size.
2. Keep request handlers thin and move reusable logic into focused service modules.
3. Use clear exception handling with precise error context.
4. Keep module boundaries explicit across auth, adapters, and MCP paths.
5. Run Python lint/test checks for changed scope.

# Outputs

- style conformance notes
- module readability improvements
- check results (`ruff check .`, `pytest`)
