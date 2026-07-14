import asyncio
import os

from app.examples import EXAMPLE_TARGET_ID, EXAMPLE_WORKSPACE_ID
from app.mcp.registry.store import mcp_server_registry, tool_registry
from app.secrets.store import secret_store
from app.target_types import KUBERNETES_TARGET_TYPE


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _seed_provider_key(env_name: str, deterministic_fallback: str) -> str | None:
    value = os.getenv(env_name)
    if value and value.strip():
        return value.strip()
    if _env_flag("LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES"):
        return deterministic_fallback
    return None


async def seed():
    provider_keys = {
        "openai_api_key": _seed_provider_key(
            "ACORNOPS_DEV_SEED_OPENAI_API_KEY",
            "sk-fake-openai-key",
        ),
        "anthropic_api_key": _seed_provider_key(
            "ACORNOPS_DEV_SEED_ANTHROPIC_API_KEY",
            "sk-fake-anthropic-key",
        ),
        "gemini_api_key": _seed_provider_key(
            "ACORNOPS_DEV_SEED_GEMINI_API_KEY",
            "fake-gemini-key",
        ),
    }

    # Seed secrets
    print("Seeding secrets...")
    try:
        workspace_scope = {"workspace_id": EXAMPLE_WORKSPACE_ID}
        for secret_name, api_key in provider_keys.items():
            if api_key:
                await secret_store.put_secret(secret_name, api_key, workspace_scope)
            else:
                print(f"Skipping {secret_name}: no dev seed key configured")
    except Exception as e:
        print(f"Secret seeding failed (maybe already exists): {e}")

    # Seed tools
    print("Seeding MCP servers...")
    try:
        existing_server = await mcp_server_registry.get_server_by_url(
            EXAMPLE_WORKSPACE_ID,
            EXAMPLE_TARGET_ID,
            "http://mock-mcp:8002/mcp",
            target_type=KUBERNETES_TARGET_TYPE,
            enabled_only=False,
        )
        if existing_server is None:
            await mcp_server_registry.create_server(
                EXAMPLE_WORKSPACE_ID,
                EXAMPLE_TARGET_ID,
                KUBERNETES_TARGET_TYPE,
                "remote-mcp-server",
                "http://mock-mcp:8002/mcp",
                True,
                "none",
            )
    except Exception as e:
        print(f"MCP server seeding failed (maybe already exists): {e}")

    print("Seeding tools...")
    try:
        existing_tool = await tool_registry.get_tool(
            EXAMPLE_WORKSPACE_ID,
            EXAMPLE_TARGET_ID,
            "get_weather",
            target_type=KUBERNETES_TARGET_TYPE,
            include_disabled=True,
        )
        if existing_tool is None:
            await tool_registry.upsert_tool(
                tool_name="get_weather",
                mcp_server_url="http://mock-mcp:8002/mcp",
                workspace_id=EXAMPLE_WORKSPACE_ID,
                target_id=EXAMPLE_TARGET_ID,
                target_type=KUBERNETES_TARGET_TYPE,
                enabled=True,
                timeout_ms=10000,
                input_schema={
                    "type": "object",
                    "properties": {"location": {"type": "string"}},
                    "required": ["location"],
                },
            )
    except Exception as e:
        print(f"Tool seeding failed (maybe already exists): {e}")


if __name__ == "__main__":
    asyncio.run(seed())
