# LLM Gateway Tech Debt Tracker

| Item | Impact | Next Action | Status |
| --- | --- | --- | --- |
| Missing streamed contract replay fixtures across providers | Medium | Keep provider-agnostic NDJSON replay fixtures current as stream contracts evolve | Closed |
| Limited edge-case coverage for remote MCP compatibility | Medium | Keep malformed-response and fallback regressions aligned with transport behavior | Closed |
| Unfixed base-OS image advisories | High-severity scanner entries; exploitability not established by the scan alone | Rebuild/rescan when distribution fixes become available. CI 40161882679 reports 44 base-OS entries with no fixed version listed and zero fixable high/critical findings; preserve the existing security gate. | Open (2026-10-25) |
| Hard schema-validation deadlines | Protect event loops from expensive schema evaluation | Disposable bounded processes in Engine/Gateway; no inline fallback | Closed; see [validation and review](completed/2026-10-31-validation-retention.md) |
