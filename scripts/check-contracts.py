import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text()


README = read("README.md")
DOC = read("docs/contracts/README.md")
MANIFEST = json.loads(read("docs/contracts/manifest.json"))
MAIN_SOURCE = read("app/main.py")
ROUTER_SOURCE = read("app/api/router.py")
LLM_SERVICE_SOURCE = read("app/llm/service.py")
CLAIMS_SOURCE = read("app/auth/claims.py")
LLM_HANDLER_SOURCE = read("app/api/handlers_llm_stream.py")
TOOL_HANDLER_SOURCE = read("app/api/handlers_tool_call.py")
MCP_ADMIN_SOURCE = read("app/api/handlers_mcp_admin.py") + read("app/api/mcp_admin_schemas.py")
MCP_ADMIN_HELPER_SOURCE = read("app/api/mcp_admin_helpers.py")
TRANSPORT_SOURCE = read("app/mcp/transports/http_transport.py")
SETTINGS_SOURCE = read("app/config/settings.py")
EXECUTION_ENGINE_CONTRACT = MANIFEST["counterparts"]["execution-engine"]
CONTROL_PLANE_CONTRACT = MANIFEST["counterparts"]["control-plane"]


failures: list[str] = []


def expect(condition: bool, message: str) -> None:
    if not condition:
        failures.append(message)


def expect_in(content: str, needle: str, message: str) -> None:
    expect(needle in content, f"{message}: missing {needle}")


expect_in(README, "[`docs/contracts/README.md`](docs/contracts/README.md)", "README contract link")
expect_in(
    README,
    "[`docs/contracts/manifest.json`](docs/contracts/manifest.json)",
    "README manifest link",
)
expect(MANIFEST["repo"] == "llm-gateway", "Manifest repo")

for heading in (
    "# LLM-Gateway Contracts",
    "## Full Platform Matrix",
    "## Platform Dependency Summary",
    "## Execution-Engine Contract",
    "## Control-Plane Contract",
    "## Generic MCP Server Contract",
):
    expect_in(DOC, heading, "Contract doc heading")

for field in (
    "run_id: str",
    "workspace_id: str",
    "target_id: str",
    "target_type: TargetType = Field(examples=TARGET_TYPE_EXAMPLES)",
    "session_id: str",
    'provider: Literal["openai", "anthropic", "gemini"]',
    "model: str",
    "messages: list[Message]",
    "tools: list[ToolSpec] = []",
    "temperature: float = 0.7",
    "max_output_tokens: int | None = None",
):
    expect_in(LLM_SERVICE_SOURCE, field, "LLM request model")

for field in (
    "iss: str",
    "aud: str",
    "run_id: str",
    "workspace_id: str",
    "target_id: str",
    "target_type: TargetType",
    "session_id: str",
    "allowed_providers: list[str] = []",
    "allowed_models: list[str] = []",
    "allowed_tools: list[str] = []",
    'allowed_tool_operations: dict[str, Literal["read", "write"]] = {}',
    "max_output_tokens: int | None = None",
):
    expect_in(CLAIMS_SOURCE, field, "Token claim model")

for field in CONTROL_PLANE_CONTRACT["runJwtPermissionFields"]:
    expect_in(DOC, field.replace("?", ""), "Documented run JWT permission field")

for needle in (
    'capability: Literal["read", "write"] = "write"',
):
    expect_in(MCP_ADMIN_SOURCE, needle, "MCP tool capability conservative default")

for needle in (
    'capability="write"',
):
    expect_in(MCP_ADMIN_HELPER_SOURCE, needle, "MCP discovery capability conservative default")

expect_in(
    DOC,
    "Missing, malformed, or newly\ndiscovered remote MCP tool capabilities default to `write`",
    "Documented MCP tool capability conservative default",
)
expect_in(
    MCP_ADMIN_SOURCE + MCP_ADMIN_HELPER_SOURCE,
    "capability is required when enabling a discovered MCP tool",
    "MCP discovered tool capability review guard",
)

for route in (
    'app.include_router(api_router, prefix="/api/v1")',
    'api_router.include_router(llm_router, prefix="/llm", tags=["llm"])',
    'api_router.include_router(tool_router, prefix="/mcp", tags=["mcp"])',
    'api_router.include_router(mcp_admin_router, prefix="/internal/mcp", tags=["internal-mcp"])',
):
    expect_in(MAIN_SOURCE + ROUTER_SOURCE, route, "API route mounting")

for documented in (
    EXECUTION_ENGINE_CONTRACT["streamPath"],
    EXECUTION_ENGINE_CONTRACT["toolCallPath"],
    *CONTROL_PLANE_CONTRACT["adminPaths"],
    "Authorization: Bearer <run-scoped-jwt>",
    "Authorization: Bearer <ADMIN_API_TOKEN>",
    CONTROL_PLANE_CONTRACT["jwksPath"],
):
    expect_in(DOC, documented, "Documented route/auth")

for needle in (
    'media_type="application/x-ndjson"',
    'detail="Scope mismatch between token and request"',
):
    expect_in(LLM_HANDLER_SOURCE, needle, "Streaming handler")

for needle in (
    'tool.source == "builtin"',
    "and server.server_name == BUILTIN_MCP_SERVER_NAME",
    "and server.server_url == BUILTIN_MCP_SERVER_URL",
    "and tool.mcp_server_url == BUILTIN_MCP_SERVER_URL",
    '"Authorization": f"Bearer {token_context.token}"',
    'if not is_builtin_tool and server and server.auth_type in ("bearer_token", "custom_header"):',
    "detail=f\"Tool {req.tool} is not permitted for this run\"",
):
    expect_in(TOOL_HANDLER_SOURCE, needle, "Tool handler")

for needle in (
    *CONTROL_PLANE_CONTRACT["serverFields"],
    *CONTROL_PLANE_CONTRACT["toolFields"],
):
    expect_in(MCP_ADMIN_SOURCE, needle, "MCP admin field")
    expect_in(DOC, needle, "Documented MCP admin field")

for needle in (
    'f"{target.connection_url}/tools/list"',
    'f"{target.connection_url}/tools/call"',
    '"method": "tools/list"',
    '"method": "tools/call"',
):
    expect_in(TRANSPORT_SOURCE, needle, "MCP transport fallback")

for needle in (
    "AUTH_ISSUER",
    "AUTH_AUDIENCE",
    "ADMIN_API_TOKEN",
    "LLM_ENABLE_DETERMINISTIC_DEV_RESPONSES",
):
    expect_in(SETTINGS_SOURCE, needle, "Gateway settings")
    expect_in(DOC, needle, "Documented gateway setting")

for field in EXECUTION_ENGINE_CONTRACT["streamResponseTypes"]:
    expect_in(DOC, f'`{{"type":"{field}"', "Documented stream response type")

for field in EXECUTION_ENGINE_CONTRACT["toolCallResponseFields"]:
    expect_in(DOC, f"`{field}`", "Documented tool-call response field")

for token in (
    CONTROL_PLANE_CONTRACT["builtinBridge"]["serverName"],
    CONTROL_PLANE_CONTRACT["builtinBridge"]["serverUrl"],
    CONTROL_PLANE_CONTRACT["builtinBridge"]["authHeader"],
    CONTROL_PLANE_CONTRACT["builtinBridge"]["scopeSource"],
    CONTROL_PLANE_CONTRACT["builtinBridge"]["callPath"],
):
    expect_in(DOC, token, "Builtin bridge doc")

for dependency in (
    "- Management console -> control plane",
    "- Control plane <-> execution-engine",
    "- Control plane <-> llm-gateway",
    "- Control plane <-> k8s-agent",
    "- Execution-engine -> llm-gateway",
):
    expect_in(DOC, dependency, "Platform dependency matrix")

if failures:
    print("Contract checks failed:\n")
    for failure in failures:
        print(f"- {failure}")
    sys.exit(1)

print("Contract checks passed.")
