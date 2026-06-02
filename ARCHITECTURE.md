# LLM Gateway Architecture

The LLM gateway is the credential and policy boundary for:

1. multi-provider LLM inference
2. NDJSON streaming responses to execution-engine
3. MCP tool brokering
4. tenant- and run-scoped authorization
5. tool registry and secret-backed remote MCP configuration

## High-Level Diagram

```mermaid
flowchart LR
    EE[execution-engine]
    GW[llm-gateway]
    Auth[JWT / JWKS validation]
    Providers[OpenAI / Anthropic / Gemini]
    MCP[MCP servers]
    Store[(Postgres / Redis / Secret backend)]

    EE --> GW
    GW --> Auth
    GW --> Providers
    GW --> MCP
    GW --> Store
```

## Detailed Diagram

```mermaid
flowchart TD
    subgraph API[FastAPI Layer]
        Main[app/main.py]
        Router[app/api/router.py]
        LLMHandler[handlers_llm_stream.py]
        ToolHandler[handlers_tool_call.py]
        MCPAdmin[handlers_mcp_admin.py]
    end

    subgraph AuthPolicy[Auth and Policy]
        JWT[auth/jwt_validator.py]
        ServiceToken[auth/service_token.py]
        Claims[auth/claims.py]
    end

    subgraph LLMPath[Inference Path]
        Normalize[llm/service.py]
        AdapterRegistry[llm/adapters/registry.py]
        OpenAI[OpenAI adapter]
        Anthropic[Anthropic adapter]
        Gemini[Gemini adapter]
    end

    subgraph MCPPath[MCP Broker Path]
        ToolRegistry[mcp.registry.store]
        ServerRegistry[mcp server registry]
        Transport[mcp.transports.http_transport]
    end

    subgraph Storage[State and Secrets]
        DB[(Postgres)]
        Redis[(Redis)]
        Secrets[DB-encrypted secrets or Vault]
    end

    subgraph External[External Systems]
        EE[execution-engine]
        Providers[LLM providers]
        RemoteMCP[Remote MCP servers]
        CP[control-plane admin client]
    end

    Main --> Router
    Router --> LLMHandler
    Router --> ToolHandler
    Router --> MCPAdmin

    LLMHandler --> JWT
    LLMHandler --> Claims
    LLMHandler --> Normalize
    Normalize --> AdapterRegistry
    AdapterRegistry --> OpenAI
    AdapterRegistry --> Anthropic
    AdapterRegistry --> Gemini

    ToolHandler --> JWT
    ToolHandler --> Claims
    ToolHandler --> ToolRegistry
    ToolHandler --> ServerRegistry
    ToolHandler --> Transport

    MCPAdmin --> ServiceToken
    MCPAdmin --> ToolRegistry
    MCPAdmin --> ServerRegistry

    ToolRegistry --> DB
    ServerRegistry --> DB
    LLMHandler --> Redis
    ToolHandler --> Redis
    LLMHandler --> Secrets
    ToolHandler --> Secrets

    OpenAI --> Providers
    Anthropic --> Providers
    Gemini --> Providers
    Transport --> RemoteMCP
    EE --> Router
    CP --> MCPAdmin
```

## Primary Responsibilities

1. validate run-scoped JWTs and enforce request scope
2. normalize execution-engine requests into provider-specific calls
3. stream provider output as normalized NDJSON events
4. broker MCP tool calls with registry lookups, schema validation, and secret-backed auth
5. expose internal admin APIs for cluster MCP server and tool management
