import uvicorn
from fastapi import FastAPI, Request
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


@app.post("/tools/list")
async def list_tools_post():
    return {"tools": TOOLS}


@app.get("/tools/list")
async def list_tools_get():
    return {"tools": TOOLS}


@app.post("/tools/call")
async def call_tool(request: Request):
    data = await request.json()
    name = data.get("name")
    arguments = data.get("arguments", {})

    if name == "get_weather":
        location = arguments.get("location", "unknown")
        return {
            "content": [{"type": "text", "text": f"The weather in {location} is sunny and 25°C."}],
            "isError": False,
        }

    return {"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True}


@app.post("/")
async def jsonrpc_root(request: Request):
    payload = await request.json()
    method = payload.get("method")
    req_id = payload.get("id")
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
        status_code=404,
    )


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8002)
