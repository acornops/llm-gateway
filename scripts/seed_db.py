import asyncio
import os

from app.examples import EXAMPLE_TARGET_ID, EXAMPLE_WORKSPACE_ID
from app.mcp.registry.models import Tool
from app.mcp.registry.store import tool_registry
from app.secrets.store import secret_store
from app.target_types import KUBERNETES_TARGET_TYPE


async def seed():
    openai_api_key = os.getenv("ACORNOPS_DEV_SEED_OPENAI_API_KEY", "sk-fake-openai-key")
    anthropic_api_key = os.getenv(
        "ACORNOPS_DEV_SEED_ANTHROPIC_API_KEY", "sk-fake-anthropic-key"
    )
    gemini_api_key = os.getenv("ACORNOPS_DEV_SEED_GEMINI_API_KEY", "fake-gemini-key")

    # Seed secrets
    print("Seeding secrets...")
    try:
        workspace_scope = {"workspace_id": EXAMPLE_WORKSPACE_ID}
        await secret_store.put_secret("openai_api_key", openai_api_key, workspace_scope)
        await secret_store.put_secret("anthropic_api_key", anthropic_api_key, workspace_scope)
        await secret_store.put_secret("gemini_api_key", gemini_api_key, workspace_scope)
    except Exception as e:
        print(f"Secret seeding failed (maybe already exists): {e}")

    # Seed tools
    print("Seeding tools...")
    tool = Tool(
        workspace_id=EXAMPLE_WORKSPACE_ID,
        target_id=EXAMPLE_TARGET_ID,
        target_type=KUBERNETES_TARGET_TYPE,
        tool_name="get_weather",
        mcp_server_url="http://mock-mcp:8002",
        enabled=True,
        timeout_ms=10000,
        input_schema={
            "type": "object",
            "properties": {"location": {"type": "string"}},
            "required": ["location"],
        },
    )
    try:
        await tool_registry.register_tool(tool)
    except Exception as e:
        print(f"Tool seeding failed (maybe already exists): {e}")


if __name__ == "__main__":
    asyncio.run(seed())
