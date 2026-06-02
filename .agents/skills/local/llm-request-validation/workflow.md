# LLM Request Validation Workflow

1. Review handler changes in `app/api` and auth modules.
2. Verify run/workspace/cluster/session scoping is preserved.
3. Validate provider adapter selection and timeout/retry behavior.
4. Confirm MCP tool call arguments are schema-validated.
5. Run `pytest` and ensure migration path (`alembic upgrade head`) is still valid.
6. Record any required control-plane coordination for admin API changes.
