# LLM Gateway Google Style Workflow

1. Review changed modules in `app/api`, `app/auth`, `app/llm`, and `app/mcp`.
2. Ensure names, control flow, and function boundaries optimize readability.
3. Keep request validation and policy checks explicit at module entry points.
4. Favor clear helper functions over repeated inline logic.
5. Run `ruff check .` and `pytest`.
6. Note any deferred cleanup opportunities.
