from prometheus_client import Counter, Gauge, Histogram

# HTTP Metrics
GATEWAY_HTTP_REQUESTS_TOTAL = Counter(
    "gateway_http_requests_total", "Total number of HTTP requests", ["endpoint", "method", "status"]
)

GATEWAY_HTTP_REQUEST_DURATION_MS = Histogram(
    "gateway_http_request_duration_ms_bucket", "HTTP request duration in milliseconds", ["endpoint"]
)

GATEWAY_READINESS_CHECK = Gauge(
    "gateway_readiness_check",
    "Readiness check status by dependency; 1 means ready and 0 means not ready",
    ["dependency", "required"],
)

# LLM Metrics
GATEWAY_STREAM_SESSIONS_ACTIVE = Gauge(
    "gateway_stream_sessions_active", "Number of active LLM stream sessions"
)

GATEWAY_LLM_PROVIDER_REQUESTS_TOTAL = Counter(
    "gateway_llm_provider_requests_total",
    "Total number of LLM provider requests",
    ["provider", "model", "status"],
)

GATEWAY_LLM_REASONING_SUMMARY_EVENTS_TOTAL = Counter(
    "gateway_llm_reasoning_summary_events_total",
    "Reasoning summary stream events emitted by the LLM gateway",
    ["provider", "model", "status"],
)

GATEWAY_LLM_REASONING_SUMMARY_UNAVAILABLE_TOTAL = Counter(
    "gateway_llm_reasoning_summary_unavailable_total",
    "Reasoning summary unavailable events emitted by the LLM gateway",
    ["provider", "model", "reason"],
)

GATEWAY_JWT_VALIDATIONS_TOTAL = Counter(
    "gateway_jwt_validations_total",
    "Total number of JWT validation attempts",
    ["status"],
)

GATEWAY_JWKS_REFRESH_TOTAL = Counter(
    "gateway_jwks_refresh_total",
    "Total number of JWKS refresh attempts",
    ["status"],
)

GATEWAY_JWKS_REFRESH_AGE_SECONDS = Gauge(
    "gateway_jwks_refresh_age_seconds",
    "Age in seconds of the most recent successful JWKS refresh",
)

# Tool Metrics
GATEWAY_TOOL_CALLS_TOTAL = Counter(
    "gateway_tool_calls_total", "Total number of tool calls", ["tool", "is_error"]
)

GATEWAY_TOOL_CALL_LATENCY_MS = Histogram(
    "gateway_tool_call_latency_ms_bucket", "Tool call latency in milliseconds", ["tool"]
)

GATEWAY_TOOL_RESULT_BYTES = Histogram(
    "gateway_tool_result_bytes",
    "Serialized tool result bytes by result view",
    ["view"],
    buckets=(256, 1024, 4096, 12288, 65536, 262144, 1048576, 2097152),
)

GATEWAY_TOOL_RESULT_NORMALIZATIONS_TOTAL = Counter(
    "gateway_tool_result_normalizations_total",
    "Tool result normalization outcomes",
    ["strategy"],
)

# Outbound dependency resilience metrics
GATEWAY_UPSTREAM_DEPENDENCY_EVENTS_TOTAL = Counter(
    "gateway_upstream_dependency_events_total",
    "Outbound dependency retry, failure, and circuit breaker events",
    ["dependency_type", "event"],
)
