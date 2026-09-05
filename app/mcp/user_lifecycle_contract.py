"""Shared numeric bounds for the control-plane MCP owner generation contract."""

# Control-plane consumers are TypeScript services, so generations must remain
# lossless in an IEEE-754 integer before they cross the JSON boundary.
MAX_MCP_MEMBERSHIP_GENERATION = 9_007_199_254_740_991
