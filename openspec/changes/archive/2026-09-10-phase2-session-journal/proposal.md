# Proposal: SessionJournal — Cross-Tool-Call LLM Memory (Phase 2 — Foundation)

> Phase 2 — SCOPE.md §3 (Phase 1 already shipped; driver layer follows in `phase2-pmp450i-driver`).
> Sibling change to `phase2-pmp450i-driver`; must be archived first.

## Intent

`lmstudio.model.act(...)` is **stateless per call**: the local LLM cannot recall which device it was investigating or what it already tried. Real Cambium/PMP cases need 5–20 sequential tool calls to diagnose a single RF degradation; today the operator must hand-hold the LLM through every step and there is no persistence across operator handoffs or server restarts. This change ships **`SessionJournal`** — a per-session JSON trace, **automatically populated by MCP middleware** plus three explicit recall tools — so the LLM has bounded cross-call memory and the operator has a single auditable artifact per investigation. **Local disk only**: zero network, air-gap safe (SCOPE §2.3).

## Scope

### In Scope

- `SessionJournal`, `SessionState`, `SessionStep` Pydantic models (one canonical file per session).
- New `Settings` fields `nora_session_journal_dir` (default `./var/sessions/`) and `nora_session_trace_max_steps` (default 50) in `src/nora/config.py`; both sanitized placeholders in `.env.example`.
- **Auto-trace middleware** in `src/nora/server.py` that wraps every `@mcp.tool` invocation: records `(timestamp, tool_name, input_args, structured_result, llm_interpretation if provided)`. The LLM is **never** responsible for logging; the middleware is.
- Three new MCP tools: `nora_session_get_state() -> SessionState`, `nora_session_set_focus(device_id: str) -> None`, `nora_session_resume(session_id: str) -> SessionState`.
- **Trace rotation policy**: when in-memory trace exceeds `nora_session_trace_max_steps`, older steps move (append-only) to `${session_id}.log.ndjson`; the canonical JSON file stays bounded.
- **Free-text sanitization** of every persisted free-text string (`llm_interpretation`, error messages, free-form args) through `nora.sanitizer.Sanitizer` on **both** write and read.
- Atomic write (`write to .tmp + os.replace`) to survive mid-write crashes.
- Strict TDD test suite (RED → GREEN per module).

### Out of Scope

- Any SNMP/SSH/ICMP driver code (lives in `phase2-pmp450i-driver`).
- `ActivityReport` (Phase 3) — will consume SessionJournal as its `process_log` later.
- Changing `nora_health`'s response shape beyond auto-trace wrapping; the four typed fields are unchanged.
- Cross-session correlation, aggregation, search, or archival beyond the simple NDJSON rotation.
- Encryption-at-rest, multi-host sync, or any network transport for the journal (air-gap stays).
- New LLM provider prompts or prompt-registry changes (no text asset touched; the registry exists if needed later).

## Capabilities

### New Capabilities

- **`session-journal`**: `SessionJournal` / `SessionState` / `SessionStep` models, atomic JSON persistence, auto-trace middleware that wraps `@mcp.tool`, three explicit recall tools, rotation policy, free-text sanitization on write+read, air-gap guarantee. Spec at `openspec/changes/phase2-session-journal/specs/session-journal/spec.md`, archived to `openspec/specs/session-journal/spec.md`.

### Modified Capabilities

None. The wrapper is additive: `nora-mcp-server`'s typed-return, stderr-only, sanitizer-boundary, no-secrets, and one-log-per-tool contracts already cover every `@mcp.tool` — existing requirements apply to the new tools without amendment. `nora_health`'s contract is preserved; the middleware only **records** the call after the existing `nora_health_impl` runs. **No delta spec needed.**

## Approach

`__main__.py` boot sequence gains one line: `init_session_journal(settings)` creates `${nora_session_journal_dir}/<session_id>.json` (UUID4 default; deterministic when resumed via `nora_session_resume`) if missing. A module-level `_journal` holds the in-memory trace. The middleware is a thin FastMCP wrapper that runs **before and after** every `@mcp.tool`: it captures `(tool_name, args)`, awaits the tool, sanitizes any free-text fields in args and result through the per-session `Sanitizer`, appends a `SessionStep` to the trace, and checks the rotation threshold (move older steps to `.log.ndjson`, persist canonical JSON atomically). The three explicit tools read/write the same journal via the public `SessionJournal` API. Boot tolerates a missing or empty journal dir (creates it) and a corrupt canonical file (typed `JournalCorruptError`; operator sees an explicit message, never silent loss). All file I/O is synchronous and local. Strict TDD per module: `models` → `persistence` → `middleware` → `tools` → rotation → sanitization; coverage ≥ 85% (Phase 1 baseline).

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `src/nora/core/session_journal.py` | New | Models, atomic persistence, rotation, middleware helper, typed exceptions. |
| `src/nora/config.py` | Modified | Two new `Settings` fields (`nora_session_journal_dir`, `nora_session_trace_max_steps`); defaults safe. |
| `src/nora/server.py` | Modified | Module-level `_journal`; auto-trace wraps existing + new `@mcp.tool`s; three new tools registered. |
| `src/nora/__main__.py` | Modified | One boot line: `init_session_journal(settings)`. |
| `.env.example` | Modified | Two new sanitized keys. |
| `tests/test_session_journal_*.py`, `tests/test_server.py` | New/Modified | Strict TDD; tmp_path fixtures for filesystem; `caplog` for stderr. |
| `var/sessions/` | New | Runtime artifact dir; gitignored (additive `.gitignore` line). |

## Risks

| Risk | Lik | Mitigation |
|------|------|------------|
| **Journal corruption** from mid-write crash or disk-full leaves unreadable JSON. | Med | Atomic write (`.tmp` + `os.replace`); typed `JournalCorruptError` on read; rotation to NDJSON keeps an immutable backup. |
| **Path injection** through `nora_session_journal_dir` (symlink, traversal). | Low-Med | `Settings` field typed as `Path`; boot resolves to absolute, ensures the directory exists and is a real directory, refuses symlinks that point outside the resolved tree. |
| **session_id collision** when an operator names a session that already exists on disk. | Low | UUID4 default is collision-safe; `set_focus`/`resume` accept explicit IDs but refuse with typed `SessionExistsError` if a closed session ID is reused while the new session is open. |
| **Auto-trace I/O stalls the MCP loop** under load (every tool call = sync disk write). | Med | Rotation amortized (single rename per threshold); atomic write is one fsync per call (acceptable for stdio MCP, low call rate); test asserts p95 overhead < 10 ms with tmpfs-backed dir. |
| **Free-text sanitization gap** — `llm_interpretation` or error strings leak a private IPv4/MAC/serial into the journal. | Low-Med | Sanitizer is invoked on every free-text field at **both** write and read; tests assert round-trip masking (write 10.0.0.5, read alias; read alias, output still alias). |
| **JSON growth** — a long-running investigation bloats the canonical file beyond rotation policy. | Low | Rotation is the policy; NDJSON append-only is bounded by the operator's disk budget; `nora_session_resume` rehydrates from NDJSON tail lazily. |

## Rollback Plan

Delete `src/nora/core/session_journal.py`; revert `src/nora/config.py` (drop two fields), `src/nora/server.py` (drop middleware + 3 tools), `src/nora/__main__.py` (drop boot line), `.env.example` (drop two keys), `tests/test_session_journal_*.py`, the additive `.gitignore` line. Phase 1 modules and `nora_health` remain untouched. Existing `var/sessions/*.json` and `.log.ndjson` are operator artifacts — left in place, never auto-deleted by the app.

## Dependencies

| | |
|---|---|
| New runtime | **None.** Stdlib only: `json`, `pathlib`, `uuid`, `datetime`, `os.replace`. No new third-party deps. |
| New dev | **None.** Existing `pytest`, `pytest-cov`, `pytest-timeout`, `ruff`, `mypy` cover all tests. |
| Reused from Phase 1 | `Settings` (`src/nora/config.py`), `Sanitizer` (`src/nora/sanitizer.py`), FastMCP `@mcp.tool` (`src/nora/server.py`), stderr-only logging contract. |
| Locked decisions (reused, not relitigated) | SessionJournal concept — two layers, local JSON, air-gap (memory `obs-ca002346cb427e5d`); Phase 2 split — journal before driver (memory `obs-2b4a21731ad3b746`); Phase 2 scope excludes ActivityReport (memory `obs-b9cc1ad585d5922b`); no LLM prompt changes needed (memory `obs-c0a0f25700260a4d`). |
| Depends on other changes | None at runtime. **Required by** `phase2-pmp450i-driver` (auto-trace middleware + `nora_session_get_state` / `set_focus` / `resume` are consumed by the new `snmp_get_pmp450i_radio_metrics` tool). That change does not re-implement the journal — it consumes it. |

## Success Criteria

- [ ] `uv sync` reproduces env; `pytest --cov=src/nora --cov-report=term-missing` exits 0; coverage ≥ 85% with the new module included.
- [ ] `ruff check .`, `ruff format --check .`, `mypy --strict src/nora` exit 0.
- [ ] Journal file persists across `nora_session_get_state` calls and across simulated server restart (close → reopen → resume reads the same canonical JSON).
- [ ] Trace rotates at `nora_session_trace_max_steps`: canonical JSON stays bounded; older steps live in `<session_id>.log.ndjson`; round-trip integrity asserted.
- [ ] Free-text sanitization verified on **both** write and read: private IPv4 / MAC / serial / hostname literals replaced by aliases before persistence; aliases survive a read-and-re-serialize round trip.
- [ ] `nora_session_resume(session_id)` works after a fresh process boot: opens a previously-closed canonical JSON and returns a populated `SessionState`; missing file → typed `SessionNotFoundError`.
- [ ] Existing `nora_health` integration: invoking `nora_health` produces one `SessionStep` in the journal with `tool_name=nora_health` and the same four-field response shape; the four typed fields are unchanged.
- [ ] Air-gap guarantee: zero network imports under `src/nora/core/session_journal.py`; lint audit / static check passes (`requests`/`httpx`/`urllib.request` absent).
- [ ] Atomic-write test: kill the writer mid-rename; reopen the journal; either old or new content present, never half-written JSON.
- [ ] Diff under the 800-line review budget; no IPs, MACs, serials, hostnames, or credentials in any artifact.