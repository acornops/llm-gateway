# Platform provider endpoint overrides

## Goal

Allow operators to route each supported provider adapter to a deployment-wide,
API-compatible base URL without changing workspace credential contracts.

## Work

- Add optional gateway settings for OpenAI, Anthropic, and Gemini base URLs.
- Pass configured URLs to the corresponding vendor SDK clients.
- Cover the client configuration with focused tests.
- Document compatibility and deployment configuration.

## Validation

- Focused provider adapter tests
- `task validate`
