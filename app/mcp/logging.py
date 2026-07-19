from urllib.parse import urlsplit


def loggable_mcp_server_origin(url: str) -> str:
    """Return an origin with credentials, paths, queries, and fragments removed."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return "[invalid MCP server URL]"
    if parsed.scheme not in {"http", "https"} or not hostname:
        return "[invalid MCP server URL]"
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    rendered_port = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{rendered_host}{rendered_port}"
