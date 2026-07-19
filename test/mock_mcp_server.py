import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

app = FastAPI()


TOOLS = [
    {
        "name": "get_weather",
        "description": "Get weather for a location.",
        "capability": "read",
        "inputSchema": {
            "type": "object",
            "properties": {
                "location": {"type": "string"}
            },
            "required": ["location"],
            "additionalProperties": False
        },
    }
]

AUTH_STATE = {
    "bearer": {"enabled": True, "requests": 0},
    "custom": {"enabled": True, "requests": 0},
}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/control/reset")
async def reset_auth_state():
    for state in AUTH_STATE.values():
        state["enabled"] = True
        state["requests"] = 0
    return AUTH_STATE


@app.post("/control/revoke/{auth_mode}")
async def revoke_auth_mode(auth_mode: str):
    if auth_mode not in AUTH_STATE:
        return JSONResponse({"detail": "unknown auth mode"}, status_code=404)
    AUTH_STATE[auth_mode]["enabled"] = False
    return AUTH_STATE[auth_mode]


@app.get("/control/stats")
async def auth_stats():
    return AUTH_STATE


async def _streamable_http(request: Request):
    payload = await request.json()
    method = payload.get("method")
    req_id = payload.get("id")
    if method == "initialize":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "acornops-mock-mcp", "version": "1.0.0"},
                },
            }
        )
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "tools/list":
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": TOOLS},
            }
        )
    if method == "tools/call":
        params = payload.get("params", {}) or {}
        name = params.get("name")
        arguments = params.get("arguments", {}) or {}
        if name == "get_weather":
            location = arguments.get("location", "unknown")
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"The weather in {location} is sunny and 25°C.",
                            }
                        ],
                        "isError": False,
                    },
                }
            )
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
                    "isError": True,
                },
            }
        )
    return JSONResponse(
        {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        },
        status_code=200,
    )


@app.post("/mcp")
async def streamable_http(request: Request):
    return await _streamable_http(request)


@app.post("/mcp/bearer")
async def streamable_http_bearer(request: Request):
    AUTH_STATE["bearer"]["requests"] += 1
    if (
        not AUTH_STATE["bearer"]["enabled"]
        or request.headers.get("authorization") != "Bearer bearer-pat"
    ):
        return JSONResponse({"detail": "bearer PAT required"}, status_code=401)
    return await _streamable_http(request)


@app.post("/mcp/custom")
async def streamable_http_custom_header(request: Request):
    AUTH_STATE["custom"]["requests"] += 1
    if (
        not AUTH_STATE["custom"]["enabled"]
        or request.headers.get("x-mcp-pat") != "custom-pat"
    ):
        return JSONResponse({"detail": "custom-header PAT required"}, status_code=403)
    return await _streamable_http(request)


@app.get("/mcp")
async def streamable_http_sse_not_supported():
    return Response(status_code=405)


@app.delete("/mcp")
async def streamable_http_sessions_not_supported():
    return Response(status_code=405)


@app.get("/mcp/{auth_mode}")
async def authenticated_streamable_http_sse_not_supported(auth_mode: str):
    del auth_mode
    return Response(status_code=405)


@app.delete("/mcp/{auth_mode}")
async def authenticated_streamable_http_sessions_not_supported(auth_mode: str):
    del auth_mode
    return Response(status_code=405)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
