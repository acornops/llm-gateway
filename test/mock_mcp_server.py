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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/mcp")
async def streamable_http(request: Request):
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


@app.get("/mcp")
async def streamable_http_sse_not_supported():
    return Response(status_code=405)


@app.delete("/mcp")
async def streamable_http_sessions_not_supported():
    return Response(status_code=405)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
