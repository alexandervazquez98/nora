# session-journal Specification

## Purpose

Defines the per-session JSON trace that gives the local LLM bounded cross-tool-call memory, plus three explicit recall tools the LLM and operator can call to steer an investigation. The auto-trace MCP middleware records every `@mcp.tool` invocation (including `nora_health`) into a canonical `${session_id}.json` file under `nora_session_journal_dir`, with rotation to NDJSON when the in-memory trace exceeds `nora_session_trace_max_steps`. Consumers: the local LLM (read-only recall), the operator (audit trail and Markdown summary), the Phase 2 PMP450I driver (declares its focus via `nora_session_set_focus`), and the Phase 3 `ActivityReport` (consumes the trace as `process_log`). All persistence is local, atomic, free-text-sanitized on both write and read, and air-gap safe — no network code path exists under `src/nora/core/session_journal/`.

## Requirements

### Requirement: R1 — Session File Is Created on First Tool Call

When `nora_session_journal_dir` is writable and no canonical file exists for the active session, the first invocation of any `@mcp.tool` MUST create `${session_id}.json`. The file MUST contain a valid `SessionState` JSON object with every required field: `session_id` (UUIDv4 string), `operator_alias` (string, ≤ 64 chars), `started_at` (UTC ISO-8601), `focus_device_id` (string or `null`), `devices_reviewed` (list of strings, possibly empty), `trace` (list of `SessionStep`, empty at creation), `last_updated` (UTC ISO-8601, equal to `started_at`).

#### Scenario: first tool call creates the canonical session file with all required fields

- **Given** `nora_session_journal_dir` exists and is writable
- **And** no `${session_id}.json` exists in that directory
- **When** any `@mcp.tool` (for example `nora_health`) is invoked
- **Then** a file `${session_id}.json` exists after the call returns
- **And** `json.load(open(file))` yields an object with all seven required keys
- **And** `session_id` parses as UUIDv4
- **And** `focus_device_id is None`, `devices_reviewed == []`, and `trace == []`

### Requirement: R2 — Auto-Trace Middleware Records Every Tool Call

The auto-trace middleware MUST wrap every `@mcp.tool` invocation. After the tool body returns (or raises), the middleware MUST append exactly one `SessionStep` to the active session with these fields: `ts` (UTC ISO-8601, captured before the tool runs), `step` (monotonically increasing int, starting at 1), `tool` (string, the registered tool name), `input` (dict of the tool's args, secret-redacted — see R10), `result_summary` (string, sanitized free-text summary of the result), `duration_ms` (int ≥ 0), `outcome` (one of `success`, `error`, `timeout`), `llm_interpretation` (`null` unless explicitly set by the caller).

#### Scenario: a successful tool call appends one SessionStep

- **Given** a fresh session with `trace == []`
- **When** any `@mcp.tool` is invoked and returns successfully
- **Then** the on-disk `trace` length is 1
- **And** the appended step has `step == 1`, `outcome == "success"`
- **And** `tool` equals the registered tool name

#### Scenario: a tool body that raises is recorded with `outcome == "error"`

- **Given** a tool body that raises an exception inside the wrapper
- **When** the wrapper catches and records
- **Then** the appended step has `outcome == "error"`
- **And** `result_summary` contains the sanitized exception message (no private IPv4/MAC/serial/hostname literal survives)

#### Scenario: `llm_interpretation` defaults to None

- **Given** no caller sets `llm_interpretation`
- **When** a tool call is recorded
- **Then** the on-disk step's `llm_interpretation` field equals `null`

### Requirement: R3 — Write-Then-Return Persistence Ordering

A `SessionStep` MUST be durable on disk BEFORE the wrapper returns the tool result to the caller. A process crash between the tool body's return and the wrapper's return MUST leave the canonical JSON file either complete-and-current or complete-and-one-step-behind; the file MUST be parseable JSON in both cases.

#### Scenario: the persistence call fires before the wrapper returns

- **Given** a spy on the persistence helper (`os.replace` or equivalent) and a tool body that returns a sentinel
- **When** the tool is invoked
- **Then** the spy fires before the wrapper returns the sentinel to the caller

#### Scenario: a simulated mid-write crash leaves a parseable file

- **Given** a tool body that has appended an in-memory step and is about to call `os.replace`
- **When** the writer is interrupted before `os.replace` (simulated by raising inside the persistence helper)
- **Then** the on-disk canonical file (old or new content) parses as valid JSON
- **And** the MCP server process remains alive

### Requirement: R4 — Atomic Writes Survive Concurrent Writers

Every canonical-file write MUST use the temp-file + `os.replace` pattern. Two concurrent writers competing for the same canonical file MUST result in a deterministic last-write-wins outcome; the file MUST never contain partial JSON.

#### Scenario: an interrupted write leaves either old or new content, never a torn mix

- **Given** a canonical file containing `V1` and a writer that creates `session.json.tmp` with `V2` but is killed before `os.replace`
- **When** the journal is reopened
- **Then** `session.json` parses as valid JSON with contents equal to `V1`
- **And** `session.json.tmp` may exist but is ignored on next open

#### Scenario: two concurrent writers both end with valid JSON

- **Given** two threads each append one step and persist through the atomic helper
- **When** both writers finish
- **Then** `session.json` parses as valid JSON
- **And** the on-disk trace length equals one of the two writers' counts (never a torn mix)

### Requirement: R5 — Trace Rotation to NDJSON

When `len(trace) > NORA_SESSION_TRACE_MAX_STEPS` (default 50), the oldest step MUST be appended (one JSON object per line) to `${session_id}.log.ndjson` and removed from the in-memory `trace`. The canonical `trace` MUST keep the most recent `NORA_SESSION_TRACE_MAX_STEPS` steps; the NDJSON file is append-only and MUST NOT be truncated by the journal itself.

#### Scenario: appending past the threshold rotates the oldest step

- **Given** `NORA_SESSION_TRACE_MAX_STEPS == 3` and a session with `len(trace) == 3` (steps 1, 2, 3)
- **When** one more tool call is recorded (creating step 4)
- **Then** `session.log.ndjson` exists and contains exactly one JSON line whose `step` integer is `1`
- **And** the canonical `trace` has length 3 and contains steps 2, 3, 4

#### Scenario: NDJSON file is append-only across multiple rotations

- **Given** two consecutive rotations each displacing one step (steps 1 → ndjson, then step 2 → ndjson)
- **When** the file is reopened
- **Then** it contains exactly two JSON lines in displacement order (step 1 first, step 2 second)

### Requirement: R6 — Free-Text Sanitization Boundary (Write AND Read)

Every free-text field on a persisted `SessionStep` (`llm_interpretation`, `result_summary`, plus any string value inside `input` whose key is not on the R10 redaction list and whose value carries identifier-bearing substrings) MUST pass through `Sanitizer.sanitize(...)` BEFORE the step is written. On every read of the canonical JSON, the same free-text fields MUST be re-sanitized through the same `Sanitizer` instance (defense in depth — a sanitize bypass at write time is still caught at read time). Structured fields (UUIDs, enum values, integers, booleans) MUST bypass sanitization.

#### Scenario: a private IPv4 in `llm_interpretation` is masked on write

- **Given** a tool call with `llm_interpretation == "investigation at 10.0.0.5"`
- **When** the step is persisted
- **Then** the on-disk `llm_interpretation` does NOT contain `10.0.0.5`
- **And** it contains a synthetic alias from `Sanitizer`

#### Scenario: re-sanitization on read masks anything that slipped past write

- **Given** a canonical file whose `result_summary` contains a literal MAC address (simulating a sanitize bypass at write time)
- **When** `nora_session_get_state()` reads the file
- **Then** the returned `SessionState.trace` does NOT contain the literal MAC
- **And** the alias is the same across write-then-read (same `Sanitizer` instance)

#### Scenario: structured fields bypass sanitization

- **Given** a step whose `tool == "nora_health"` (enum), `step == 7` (int), and `duration_ms == 12` (int)
- **When** the step is written and read back
- **Then** all three values are byte-identical to their original forms (no alias substitution)

### Requirement: R7 — Three Explicit Recall Tools

The MCP server MUST expose exactly three `@mcp.tool`-registered functions operating on the active session: `nora_session_get_state`, `nora_session_set_focus`, and `nora_session_resume`.

#### Scenario: `nora_session_get_state` returns the current SessionState and is idempotent

- **Given** an active session with two recorded steps
- **When** `nora_session_get_state()` is called twice in succession
- **Then** both calls return an equivalent `SessionState`
- **And** neither call mutates the canonical file

#### Scenario: `nora_session_get_state` on a missing file returns empty state and creates one

- **Given** no `${session_id}.json` exists
- **When** `nora_session_get_state()` is called
- **Then** it returns a `SessionState` with `trace == []`
- **And** a new canonical file is created at the resolved path

#### Scenario: `nora_session_set_focus` records the device and appends to `devices_reviewed`

- **Given** an active session with `focus_device_id is None` and `devices_reviewed == []`
- **When** `nora_session_set_focus("ap-7400-01")` is called
- **Then** the persisted state has `focus_device_id == "ap-7400-01"`
- **And** `devices_reviewed == ["ap-7400-01"]`
- **And** `last_updated` is strictly greater than the previous `last_updated`

#### Scenario: `nora_session_set_focus` is idempotent on the same device_id

- **Given** `devices_reviewed == ["ap-7400-01"]`
- **When** `nora_session_set_focus("ap-7400-01")` is called again
- **Then** `devices_reviewed` is still `["ap-7400-01"]` (length 1, not 2)

#### Scenario: `nora_session_resume` loads a previously-saved session

- **Given** a canonical `${session_id}.json` exists with two recorded steps
- **When** `nora_session_resume(session_id)` is called
- **Then** the returned `SessionState` matches the on-disk contents
- **And** subsequent tool calls append to that resumed session, not a fresh one

#### Scenario: `nora_session_resume` raises `SessionNotFoundError` on a missing file

- **Given** no file at the resolved path for `session_id`
- **When** `nora_session_resume(session_id)` is called
- **Then** a `SessionNotFoundError` is raised
- **And** the active session is NOT mutated

### Requirement: R8 — Air-Gap Guarantee (No Network Imports)

No source file under `src/nora/core/session_journal/` (the package this spec owns) MUST import `requests`, `httpx`, `urllib.request`, `socket`, `ssl`, or `http.client`. This MUST be verifiable by both a static scan and a runtime test that mocks every banned symbol.

#### Scenario: the journal package has zero network imports

- **Given** every `.py` file under `src/nora/core/session_journal/`
- **When** a static grep scans for the banned import strings
- **Then** zero matches are found
- **And** a runtime test that mocks `socket.socket`, `urllib.request.urlopen`, and `httpx.get`, then invokes the full middleware, shows none of those mocks are called

### Requirement: R9 — Session ID Is Immutable

`SessionState.session_id` MUST be set exactly once at creation and MUST NOT change afterward. `nora_session_resume(session_id)` MUST load the named file as the active session but MUST NOT mutate the in-memory `session_id` of any other session; subsequent writes after resume go to the resumed session's canonical file.

#### Scenario: `session_id` is stable across many writes

- **Given** a fresh session with `session_id == S`
- **When** 5 tool calls are recorded
- **Then** every persisted step's containing `SessionState.session_id` equals `S`

#### Scenario: `resume` switches the active session without touching the previous one

- **Given** session A is active with 2 recorded steps and session B exists on disk with 1 step
- **When** `nora_session_resume("B")` is called and one tool runs
- **Then** the appended step is written to `${B}.json`, not `${A}.json`
- **And** session A's canonical file is byte-identical to its pre-resume state

### Requirement: R10 — Tool Input/Output Secret Redaction by Parameter Name

The middleware MUST redact any input argument whose parameter NAME matches the frozen redaction list, regardless of the argument's value. The redaction list (frozen by this spec) is: `community`, `community_string`, `auth_password`, `auth_key`, `priv_password`, `priv_key`, `password`, `ssh_password`, `api_key`, `token`, `secret`. A redacted value MUST be replaced with the string `"[REDACTED]"`. The same name match MUST apply recursively inside nested dicts and lists. The redaction happens BEFORE sanitization so the redacted marker is never re-aliased.

#### Scenario: a top-level SNMP community string is redacted before write

- **Given** a tool call with `kwargs == {"device_id": "ap-7400-01", "community": "private"}`
- **When** the step is persisted
- **Then** `json.load(file)["trace"][-1]["input"]` equals `{"device_id": "ap-7400-01", "community": "[REDACTED]"}`
- **And** the literal `"private"` is not present anywhere in the file

#### Scenario: a nested `api_key` is redacted while its siblings pass through

- **Given** a tool call with `kwargs == {"creds": {"api_key": "sk-test-1234", "region": "us-east"}}`
- **When** the step is persisted
- **Then** `input["creds"]["api_key"] == "[REDACTED]"` and `input["creds"]["region"] == "us-east"`

### Requirement: R11 — Failure Containment on Corrupt Files

A canonical file containing bytes that are NOT valid JSON MUST cause a typed `JournalCorruptError` to be raised at read time. The MCP server MUST NOT crash. On the next write attempt, the journal MUST auto-recover by archiving the corrupt file (rename to `${session_id}.corrupt-<unix-timestamp>.json`) and creating a fresh canonical file.

#### Scenario: reading a corrupt file raises `JournalCorruptError` without crashing

- **Given** `${session_id}.json` contains bytes that are NOT valid JSON
- **When** `nora_session_get_state()` is called
- **Then** a `JournalCorruptError` is raised
- **And** the MCP server is still alive (a follow-up `nora_health` call returns its normal 4-tuple)

#### Scenario: the next write auto-recovers by archiving the corrupt file

- **Given** a corrupt `${session_id}.json` (from the previous scenario)
- **When** any `@mcp.tool` is invoked after the read failure
- **Then** a file matching `${session_id}.corrupt-*.json` exists with the original bad bytes
- **And** a fresh `${session_id}.json` is written with a valid empty `SessionState` plus the new step

### Requirement: R12 — Read-Only Failure Path Records Without Mutation

When an `@mcp.tool` returns a soft-error response (no exception raised; the response itself signals error, e.g. `nora_health` returning `connectivity: unavailable`), the middleware MUST record the call with `outcome == "error"` and MUST NOT mutate the tool's returned response to "fix" the error. The response shape is owned by `nora-mcp-server` and stays byte-identical.

#### Scenario: `nora_health` returning `connectivity: unavailable` records the call without changing the response

- **Given** the LLM provider is unreachable
- **When** `nora_health` is invoked
- **Then** the tool response still equals `{"version", "active_provider", "connectivity": "unavailable", "env_loaded"}` (the four-field shape from `nora-mcp-server` is preserved)
- **And** the journal contains a step with `tool == "nora_health"` and `outcome == "error"`

---

### Requirement: R13 — `operator_alias` Comes From Env (SHOULD)

`operator_alias` SHOULD be derived from the env var `NORA_OPERATOR_ALIAS` (default `"anonymous"`); the value SHOULD be a non-empty string of length ≤ 64. The env-var surface inherits `secure-configuration`'s locked-no-hardcoded-values rule.

#### Scenario: env var overrides the default operator_alias

- **Given** `NORA_OPERATOR_ALIAS == "noc-night-shift"` (a synthetic placeholder)
- **When** a new session is created
- **Then** the canonical file's `operator_alias == "noc-night-shift"`

### Requirement: R14 — `last_updated` Re-Stamped on Read AND Write (SHOULD)

`SessionState.last_updated` SHOULD be re-stamped on every read of the canonical file and every write that mutates state. This is a cheap, useful signal for staleness checks by other tools (e.g. `ActivityReport` in Phase 3).

#### Scenario: a read-only call advances `last_updated`

- **Given** a session with `last_updated == T0`
- **When** `nora_session_get_state()` is called
- **Then** the next persisted write records `last_updated > T0`

### Requirement: R15 — Journal Disable Switch (SHOULD)

When the env var `NORA_SESSION_JOURNAL_ENABLED == "false"`, the auto-trace middleware MUST skip recording entirely (no file created, no step appended) and the three explicit tools MUST raise a typed `JournalDisabledError`. The disable switch is for operator-controlled opt-out (e.g. ephemeral demo sessions).

#### Scenario: the disable switch skips auto-trace

- **Given** `NORA_SESSION_JOURNAL_ENABLED == "false"`
- **When** any `@mcp.tool` is invoked
- **Then** no file appears under `nora_session_journal_dir`
- **And** the tool's typed response is unchanged

#### Scenario: the disable switch makes explicit tools raise

- **Given** `NORA_SESSION_JOURNAL_ENABLED == "false"`
- **When** `nora_session_get_state()` is called
- **Then** a `JournalDisabledError` is raised

### Requirement: R16 — `get_state` Accepts `include_rotated` (SHOULD)

`nora_session_get_state(include_rotated: bool = False) -> SessionState` SHOULD include the rotated NDJSON steps in the returned `trace` when `include_rotated` is true. Default is `False` for backward compatibility.

#### Scenario: rotated steps appear in chronological order when requested

- **Given** 5 displaced steps in `session.log.ndjson` and 3 fresh steps in canonical `trace`
- **When** `nora_session_get_state(include_rotated=True)` is called
- **Then** the returned `trace` has length 8 (5 rotated + 3 canonical)
- **And** the displaced steps appear first, in `step` order

### Requirement: R17 — `nora_session_summarize()` Returns Markdown (SHOULD)

`nora_session_summarize() -> str` SHOULD return a non-empty Markdown projection of the current state covering the `focus_device_id`, `devices_reviewed`, and the last 10 step summaries. This is the operator-facing "formato digerible" view.

#### Scenario: summarize returns non-empty Markdown with the expected fields

- **Given** an active session with `focus_device_id`, two entries in `devices_reviewed`, and three recorded steps
- **When** `nora_session_summarize()` is called
- **Then** the returned string is non-empty Markdown
- **And** it contains the `focus_device_id`, both device IDs, and at least one tool name from the trace

### Requirement: R18 — Journal File Mode 0o600 on POSIX (SHOULD)

The canonical `${session_id}.json` and `${session_id}.log.ndjson` files SHOULD be created with POSIX mode `0o600` (owner read/write only). On non-POSIX filesystems the requirement is N/A and MUST NOT error.

#### Scenario: canonical file is owner-only on POSIX

- **Given** a fresh session on a POSIX filesystem
- **When** the canonical file is created
- **Then** `Path(file).stat().st_mode & 0o777 == 0o600`

---

### Requirement: R19 — Gzip Rotation After 24h (MAY)

The journal MAY compress rotated NDJSON files with `gzip` after they have existed for 24 hours, producing `${session_id}.log.ndjson.gz` and removing the plain `.ndjson`.

#### Scenario: an old NDJSON is gzipped on the next rotation

- **Given** `session.log.ndjson` exists with `mtime` 25 hours ago
- **When** a new rotation occurs
- **Then** `session.log.ndjson.gz` exists and decodes to the original NDJSON content
- **And** the plain `.ndjson` file no longer exists

### Requirement: R20 — Cross-Session Search (MAY)

The journal MAY expose `nora_session_search(query: str) -> list[SessionSummary]` which scans all canonical files in `nora_session_journal_dir` and returns session summaries whose sanitized free-text fields match `query`. Search is offline and local (no FTS index required).

#### Scenario: search returns only matching sessions

- **Given** three canonical files, one whose trace contains the alias `RADIO_NODE_A` and two that do not
- **When** `nora_session_search("RADIO_NODE_A")` is called
- **Then** exactly one `SessionSummary` is returned
- **And** the summary identifies the matching `session_id`

### Requirement: R21 — Encryption-at-Rest Hook (MAY)

The journal MAY accept an `encryption_key: bytes | None` parameter that, when provided (length 32), encrypts the canonical JSON using a project-approved symmetric cipher (e.g. AES-GCM) before persistence and decrypts on read. This is a hook for a future change; the spec only defines the interface and the on-disk contract.

#### Scenario: an encryption key produces ciphertext on disk and plaintext on read

- **Given** an `encryption_key` of length 32 is configured
- **When** a session is created and one step is recorded
- **Then** the canonical file's raw bytes do NOT parse as JSON without first decrypting
- **And** `nora_session_get_state()` returns the expected `SessionState`

---

## Cross-References

- `nora-mcp-server` — the auto-trace middleware composes additively with that spec's typed-return, stderr-only, sanitizer-boundary, no-secrets, and one-log-per-tool contracts. `nora_health`'s four-field response shape is preserved (R12). The middleware only **records** after the existing `nora_health_impl` runs; no delta spec is needed for `nora-mcp-server`.
- `telemetry-sanitizer` — `Sanitizer` is the upstream dependency invoked on every free-text field at both write and read time (R6). R10's secret redaction by parameter name is a separate concern handled by the middleware's name-keyed redaction list, and runs before sanitization so the `[REDACTED]` marker is never re-aliased.
- `secure-configuration` — `nora_session_journal_dir` and `nora_session_trace_max_steps` are `Settings` fields whose env-var surface inherits the locked-no-hardcoded-values rule; `.env.example` MUST grow with sanitized placeholders for both keys (no real IPs, paths, or counts).
- `phase2-pmp450i-driver` — downstream Phase 2 sibling change; its new `snmp_get_pmp450i_radio_metrics` tool MUST call `nora_session_set_focus(device_id)` to declare its investigation target and SHOULD use `nora_session_get_state` to recall prior steps across calls. The driver does NOT re-implement the journal — it consumes it.
