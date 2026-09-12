# Delta for session-journal

## REMOVED Requirements

### Requirement: R1 — Session File Is Created on First Tool Call

(Reason: Locked decision 3 — SessionJournal is removed; no journal file is created on any tool call.)

### Requirement: R2 — Auto-Trace Middleware Records Every Tool Call

(Reason: Locked decision 3 — `_AutoTraceMiddleware` is removed from `src/nora/server.py`.)

### Requirement: R3 — Write-Then-Return Persistence Ordering

(Reason: Locked decision 3 — journal is removed; no write-then-return ordering applies.)

### Requirement: R4 — Atomic Writes Survive Concurrent Writers

(Reason: Locked decision 3 — journal is removed; no writes to coordinate.)

### Requirement: R5 — Trace Rotation to NDJSON

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R6 — Free-Text Sanitization Boundary (Write AND Read)

(Reason: Locked decision 3 — journal is removed. Sanitizer boundary remains in intervention-memory tools.)

### Requirement: R7 — Three Explicit Recall Tools

(Reason: Locked decision 3 — `nora_session_get_state`, `nora_session_set_focus`, `nora_session_resume` are removed.)

### Requirement: R8 — Air-Gap Guarantee (No Network Imports)

(Reason: Locked decision 3 — journal is removed; air-gap guarantee still holds across `src/nora/intervention_memory/` and `src/nora/drivers/`.)

### Requirement: R9 — Session ID Is Immutable

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R10 — Tool Input/Output Secret Redaction by Parameter Name

(Reason: Locked decision 3 — journal is removed. The 11-key redaction list still applies via the intervention-memory sanitizer.)

### Requirement: R11 — Failure Containment on Corrupt Files

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R12 — Read-Only Failure Path Records Without Mutation

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R13 — `operator_alias` Comes From Env (SHOULD)

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R14 — `last_updated` Re-Stamped on Read AND Write (SHOULD)

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R15 — Journal Disable Switch (SHOULD)

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R16 — `get_state` Accepts `include_rotated` (SHOULD)

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R17 — `nora_session_summarize()` Returns Markdown (SHOULD)

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R18 — Journal File Mode 0o600 on POSIX (SHOULD)

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R19 — Gzip Rotation After 24h (MAY)

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R20 — Cross-Session Search (MAY)

(Reason: Locked decision 3 — journal is removed.)

### Requirement: R21 — Encryption-at-Rest Hook (MAY)

(Reason: Locked decision 3 — journal is removed.)