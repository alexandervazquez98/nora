# session-journal Specification

## Purpose

Defines the per-session JSON trace that gives the local LLM bounded cross-tool-call memory, plus three explicit recall tools the LLM and operator can call to steer an investigation. The auto-trace MCP middleware records every `@mcp.tool` invocation (including `nora_health`) into a canonical `${session_id}.json` file under `nora_session_journal_dir`, with rotation to NDJSON when the in-memory trace exceeds `nora_session_trace_max_steps`. Consumers: the local LLM (read-only recall), the operator (audit trail and Markdown summary), the Phase 2 PMP450I driver (declares its focus via `nora_session_set_focus`), and the Phase 3 `ActivityReport` (consumes the trace as `process_log`). All persistence is local, atomic, free-text-sanitized on both write and read, and air-gap safe — no network code path exists under `src/nora/core/session_journal/`.

## Requirements

## Cross-References

- `nora-mcp-server` — the auto-trace middleware composes additively with that spec's typed-return, stderr-only, sanitizer-boundary, no-secrets, and one-log-per-tool contracts. `nora_health`'s four-field response shape is preserved (R12). The middleware only **records** after the existing `nora_health_impl` runs; no delta spec is needed for `nora-mcp-server`.
- `telemetry-sanitizer` — `Sanitizer` is the upstream dependency invoked on every free-text field at both write and read time (R6). R10's secret redaction by parameter name is a separate concern handled by the middleware's name-keyed redaction list, and runs before sanitization so the `[REDACTED]` marker is never re-aliased.
- `secure-configuration` — `nora_session_journal_dir` and `nora_session_trace_max_steps` are `Settings` fields whose env-var surface inherits the locked-no-hardcoded-values rule; `.env.example` MUST grow with sanitized placeholders for both keys (no real IPs, paths, or counts).
- `phase2-pmp450i-driver` — downstream Phase 2 sibling change; its new `snmp_get_pmp450i_radio_metrics` tool MUST call `nora_session_set_focus(device_id)` to declare its investigation target and SHOULD use `nora_session_get_state` to recall prior steps across calls. The driver does NOT re-implement the journal — it consumes it.
