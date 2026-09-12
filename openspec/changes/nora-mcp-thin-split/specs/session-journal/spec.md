# Delta for session-journal (ARCHIVED)

## Purpose

The `session-journal` capability is being archived as part of `nora-mcp-thin-split`. Locked decision 3 eliminates `SessionJournal` and the auto-trace MCP middleware from the codebase entirely; no replacement is shipped inside NORA. The corresponding requirement set is removed with no migration path inside NORA.

## REMOVED Requirements

All requirements previously declared under `openspec/specs/session-journal/spec.md` (R1 through R21) are removed.

(Reason: Locked decision 3 — the LLM/journal capability pair was used only by `nora_health` and the four `nora_session_*` tools, which are themselves removed by `nora-mcp-thin-split`. Per the openchat deployment model, the LLM and session-recall surface lives on the openchat UIWeb client, not inside NORA.)
(Migration: None inside NORA. `src/nora/core/*` (5 files) is deleted; the `_AutoTraceMiddleware` class and the `init_session_journal` re-export are removed from `src/nora/server.py`; `nora_session_set_focus` is dropped from `src/nora/drivers/snmp_pmp450i/driver.py:115`. Any openchat-side session-recall concerns are out of NORA's scope.)
