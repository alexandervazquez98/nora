# Design: SessionJournal — Cross-Tool-Call LLM Memory (Phase 2 — Foundation)

> Sibling to `phase2-pmp450i-driver`; this change ships first. Spec at
> `openspec/changes/phase2-session-journal/specs/session-journal/spec.md` is the
> authoritative contract (21 requirements, 37 scenarios). This design assumes
> `strict-TDD` (RED → GREEN → REFACTOR per module) and a review budget of
> 800 lines / 400-LOC changed per PR slice.

## 1. Module layout

```
src/nora/
  core/
    __init__.py            # Re-exports the public API surface
    session_journal.py     # SessionJournal class (public API + typed exceptions)
    session_models.py      # Pydantic models: SessionState, SessionStep, Outcome
    session_paths.py       # Atomic write + 0o600 mode + dir validation
    session_redaction.py   # R10 frozen list + recursive walker (per spec)
    session_rotation.py    # NDJSON rotation helper (R5)
  config.py                # Modified: +3 Settings fields
  server.py                # Modified: FastMCP Middleware + 3 @mcp.tool + _journal
  __main__.py              # Modified: +init_session_journal(settings)
```

Boundary rationale (1 line each):
- `session_models.py` is pure data; isolated so Pydantic validation is testable
  without any I/O or sanitizer coupling.
- `session_paths.py` owns the only OS calls in the package (temp file,
  `os.replace`, `os.chmod`, symlink resolution); everything else is pure
  Python so unit tests run hermetically.
- `session_redaction.py` is a recursive walker over arbitrary JSON-shaped
  dicts/lists; it MUST be importable without `pydantic` so it can also run
  on untrusted `result` payloads before `SessionStep` is built.
- `session_rotation.py` owns the NDJSON append contract (R5); isolated so
  its tests don't drag the whole journal into scope.
- `session_journal.py` is the public API: `init_session_journal`,
  `get_journal`, `record_step`, `get_state`, `set_focus`, `resume`,
  `summarize`, and the typed exception hierarchy. This is the ONLY module
  `server.py` imports from `nora.core.*`.

Air-gap boundary (R8): the entire `src/nora/core/` subtree MUST NOT import
`requests`, `httpx`, `urllib.request`, `socket`, `ssl`, or `http.client`.
Enforced by an AST scan test (see §6).

## 2. Class & function signatures

```python
# src/nora/core/session_models.py
from __future__ import annotations
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from pydantic import BaseModel, Field

Outcome = Literal["success", "error", "timeout"]  # R2

class SessionStep(BaseModel):
    ts: datetime                       # UTC ISO-8601; captured BEFORE tool runs (R2)
    step: int                          # monotonically increasing, starts at 1 (R2)
    tool: str                          # registered tool name
    input: dict[str, Any]              # redacted by R10 walker, then sanitized
    result_summary: str                # sanitized free-text summary (R6)
    duration_ms: int = Field(ge=0)
    outcome: Outcome
    llm_interpretation: str | None = None

class SessionState(BaseModel):
    session_id: str                    # UUIDv4; immutable after creation (R9)
    operator_alias: str = Field(max_length=64)  # R13
    started_at: datetime
    focus_device_id: str | None = None
    devices_reviewed: list[str] = Field(default_factory=list)
    trace: list[SessionStep] = Field(default_factory=list)
    last_updated: datetime             # re-stamped on read AND write (R14)
```

```python
# src/nora/core/session_journal.py
from __future__ import annotations
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel
from nora.config import Settings
from nora.core.session_models import SessionState, SessionStep
from nora.core.session_paths import atomic_write_json
from nora.core.session_redaction import REDACTION_LIST, redact
from nora.core.session_rotation import rotate_if_needed
from nora.sanitizer import Sanitizer

logger = logging.getLogger("nora.core.session_journal")

# Typed exception hierarchy (R7).
class SessionJournalError(Exception): ...
class SessionNotFoundError(SessionJournalError): ...
class JournalCorruptError(SessionJournalError): ...
class JournalDisabledError(SessionJournalError): ...

REDACTION_LIST: Final[frozenset[str]]  # exported from session_redaction

class SessionJournal:
    def __init__(
        self,
        journal_dir: Path,
        max_trace_steps: int,
        sanitizer: Sanitizer,
        enabled: bool = True,
    ) -> None: ...

    # --- lifecycle ---
    @classmethod
    def for_settings(cls, settings: Settings, sanitizer: Sanitizer) -> "SessionJournal": ...

    # --- primary API (used by middleware + 3 explicit tools) ---
    def record_step(
        self,
        *,
        tool: str,
        input_args: dict[str, Any],
        result_summary: str,
        duration_ms: int,
        outcome: Outcome,
        ts: datetime,
        llm_interpretation: str | None = None,
    ) -> SessionStep: ...

    def get_state(self, *, include_rotated: bool = False) -> SessionState: ...

    def set_focus(self, device_id: str) -> SessionState: ...

    def resume(self, session_id: str) -> SessionState: ...

    def summarize(self) -> str: ...  # R17 — Markdown projection

    # --- internals (package-private; tested but not exported in __all__) ---
    def _load_or_create(self) -> SessionState: ...
    def _persist(self, state: SessionState) -> None: ...
    def _recover_from_corrupt(self, path: Path) -> None: ...
    def _rehydrate_rotated(self, state: SessionState) -> list[SessionStep]: ...
```

```python
# src/nora/core/session_paths.py
import os
from pathlib import Path
from typing import Any
def atomic_write_json(path: Path, payload: dict[str, Any]) -> None: ...
def ensure_journal_dir(path: Path) -> Path: ...   # resolves, mkdir 0o700, refuse symlinks
def canonical_path(journal_dir: Path, session_id: str) -> Path: ...
def ndjson_path(journal_dir: Path, session_id: str) -> Path: ...

# src/nora/core/session_redaction.py
REDACTION_LIST: frozenset[str]
def redact(value: Any) -> Any: ...   # recursive walker, returns a new structure

# src/nora/core/session_rotation.py
def rotate_if_needed(
    state: SessionState, ndjson_path: Path, max_steps: int
) -> SessionState: ...
```

```python
# src/nora/server.py  (additive, no behaviour change to nora_health)
from nora.core.session_journal import SessionJournal

_journal: SessionJournal | None = None
def init_session_journal(settings: Settings) -> SessionJournal: ...
def get_journal() -> SessionJournal: ...   # raises JournalDisabledError if NORA_SESSION_JOURNAL_ENABLED=false

class _AutoTraceMiddleware(fastmcp.Middleware):  # fastmcp>=3.2 base class
    def __init__(self, journal: SessionJournal) -> None: ...
    async def on_call_tool(self, context, call_next) -> Any: ...

# 3 explicit recall tools (R7)
@mcp.tool
def nora_session_get_state(include_rotated: bool = False) -> dict[str, Any]: ...

@mcp.tool
def nora_session_set_focus(device_id: str) -> dict[str, Any]: ...

@mcp.tool
def nora_session_resume(session_id: str) -> dict[str, Any]: ...

# Convenience projection (R17) — same registry surface
@mcp.tool
def nora_session_summarize() -> str: ...
```

## 3. Middleware integration strategy

**Pick: FastMCP `Middleware` base class (`on_call_tool` hook) + module-level
singleton.** FastMCP 3.2+ exposes a `Middleware` ABC with
`on_call_tool(context, call_next) -> ToolResult`. We subclass it, hold a
`SessionJournal` reference, and register one instance via `mcp.add_middleware()`
in `__main__` boot. The journal is a module-level `_journal` (matches the
existing `_current_settings` / `_current_provider` / `_sanitizer` pattern in
`server.py`); `get_journal()` returns it or raises `JournalDisabledError`
when `NORA_SESSION_JOURNAL_ENABLED == "false"`.

Rejected:
- **Decorator factory** (`@mcp_auto_trace`) — would require re-decorating the
  existing `nora_health`; the `@mcp.tool` registration already happened, so
  the wrapper wouldn't fire uniformly across tools.
- **Monkey-patch at boot** — opaque, untestable, fragile to FastMCP upgrades.

### Code-path trace (one `@mcp.tool` invocation)

```
JSON-RPC stdin
  └─► FastMCP dispatcher
        └─► Middleware chain: _AutoTraceMiddleware.on_call_tool
              ├─ start = time.monotonic()                 # duration_ms source
              ├─ ts     = datetime.now(timezone.utc)       # R2: before tool runs
              ├─ tool   = context.message.name             # e.g. "nora_health"
              ├─ args   = context.message.arguments or {}  # dict, may be None
              ├─ try:
              │     result = await call_next(context)      # the actual @mcp.tool body
              │     summary = derive_summary(result)        # e.g. "connectivity=ok"
              │   except BaseException as exc:
              │     summary = sanitizer.sanitize(str(exc)).text  # R2 error path
              │     raise                                   # R12: do not swallow
              ├─ journal.record_step(                      # R3: persist BEFORE return
              │     tool=tool, input_args=args, result_summary=summary,
              │     duration_ms=int((monotonic-start)*1000),
              │     outcome="success"|"error", ts=ts,
              │ )
              └─ return result                              # R12: byte-identical shape
```

Three properties fall out for free:
- **R3 ordering**: `record_step` (which calls `atomic_write_json`) returns
  BEFORE the middleware returns control to FastMCP. A process crash
  between the tool body's return and `record_step` returns leaves the
  previous good state on disk; a crash during the write leaves either old
  or new content (R4 atomicity).
- **R12 byte-identical response**: the middleware never mutates `result`.
  It only observes.
- **R2 error recording + R3 re-raise**: the `except` records with
  `outcome="error"` then `raise`. The exception bubbles to FastMCP, which
  serialises it as a JSON-RPC error. The journal is durably one step
  ahead of the tool body's intent.

### `ts` capture — BEFORE the tool body

`ts` is captured at the top of `on_call_tool`, before `await call_next`.
Per R2: "ts (UTC ISO-8601, captured before the tool runs)". The spec uses
`ts` as the canonical "when did this call start" timestamp; `duration_ms`
is the only timing tied to the tool body. If `ts` were captured after,
then a slow tool would have a misleading "started" time.

### Tool body raise — catch, record, re-raise

The middleware catches `BaseException`-as-`Exception` (NOT `BaseException`,
so `KeyboardInterrupt` / `SystemExit` still propagate), records with
`outcome="error"`, then re-raises. The spec scenario is explicit
("records with `outcome == "error"`") and the MCP server MUST stay alive
(R11). A swallowed exception would break the LLM's tool contract.

## 4. Concurrency model

**Pick: Option D — lock-free, rely on `os.replace` atomicity.**

FastMCP is async; a stdio MCP server typically has one client (Claude
Desktop), but the spec explicitly addresses multi-writer scenarios (R4).
R4 says "deterministic last-write-wins" is acceptable. We accept it:

| Concern | Handling |
|---|---|
| Two `@mcp.tool` calls in the same process | Each `record_step` is a sync call: in-memory mutate → `atomic_write_json`. CPython's GIL makes the in-memory `state.trace.append` + `step` increment atomic per statement. Two callers may both compute `step = state.trace[-1].step + 1` from the same base; whichever `os.replace` lands last wins, and the on-disk `step` is still monotonic. |
| Crash mid-write | `os.replace` is atomic on POSIX and on Windows (since 3.3); partial `*.json.tmp` is left orphaned and ignored on next open. |
| Multi-host | Out of scope (air-gap, single host). |

Rejected:
- **`asyncio.Lock` per session** — `record_step` is sync; an async lock
  around a sync write buys nothing and adds overhead.
- **`threading.Lock` per session** — adds overhead; the GIL already
  serialises in-memory state mutations; the only race is at the file
  boundary which `os.replace` resolves.
- **`fcntl.flock` / `msvcrt`** — cross-platform pain for zero practical
  benefit on stdio MCP.

R4 acceptance is documented in the `record_step` docstring; the
`test_concurrent_writers_both_end_with_valid_json` test in §6 pins the
behaviour explicitly.

## 5. Sanitization + redaction ordering (R6 + R10)

Per call to `record_step` (one step, the `input` dict example):

```python
def record_step(self, *, tool, input_args, result_summary, ...):
    # 1. R10 — name-keyed redaction. Walks dicts/lists to any depth.
    #    Marker "[REDACTED]" replaces the value; the literal value
    #    never reaches disk.
    redacted_input = redact(input_args)

    # 2. R6 — sanitizer on free-text strings. The result_summary
    #    is a free-text string; `input` is now redacted but free-text
    #    values (e.g. notes, comments) inside the redacted dict are
    #    still sanitized via sanitizer.sanitize(...).text. We keep
    #    one Sanitizer instance per SessionJournal so aliases are
    #    stable within a session.
    sanitized_input = _sanitize_tree(redacted_input, self._sanitizer)
    sanitized_summary = self._sanitizer.sanitize(result_summary).text

    # 3. Pydantic validation. Structured fields (UUIDs, ints, enums)
    #    bypass sanitization entirely — they never enter the
    #    Sanitizer's regex sweep.
    step = SessionStep(
        ts=ts, step=next_step, tool=tool, input=sanitized_input,
        result_summary=sanitized_summary, duration_ms=duration_ms,
        outcome=outcome, llm_interpretation=llm_interpretation,
    )

    # 4. Atomic write — temp + os.replace, mode 0o600 on POSIX.
    state.trace.append(step)
    state.last_updated = datetime.now(timezone.utc)
    self._persist(state)
```

This order matches R6 (`Sanitizer` on write AND read) and R10 (redaction
BEFORE sanitization, so `[REDACTED]` is never re-aliased). The
re-sanitization on read happens inside `get_state()`: it re-runs
`sanitizer.sanitize(...)` on `result_summary` and free-text fields of
`input` after Pydantic rehydration. This is the "defense in depth" the
spec demands for the read path.

## 6. TDD strategy

| Layer | What | Approach |
|---|---|---|
| Unit | models, atomic write, redaction walker, rotation, summarize, sanitize-on-read | `pytest` only; no FastMCP; `tmp_path` for the journal dir; `monkeypatch.setenv` for `NORA_*`; `caplog` for structured log lines; `freezegun.freeze_time` for `last_updated` (R14). |
| Integration | middleware wraps `nora_health`; 3 explicit recall tools; air-gap audit | Real FastMCP server: build a fresh `FastMCP("nora-test")`, register the 3 tools + the middleware, invoke via `await mcp.call_tool(...)` against an in-process `Client`. |
| Property | `SessionState` round-trip | Hypothesis: `state == load(save(state))` after arbitrary mutation sequences. |
| Air-gap | no banned imports | AST scan over `src/nora/core/*.py` for `requests` / `httpx` / `urllib.request` / `socket` / `ssl` / `http.client`; also a runtime test that patches the banned symbols and asserts the middleware never touches them. |

### Tests pinned from the 37 spec scenarios (strict TDD order)

```
tests/test_session_models.py
  - test_session_step_carries_seven_required_fields              # R2 (1)
  - test_session_state_has_all_required_keys_at_creation         # R1 (1)
  - test_outcome_literal_accepts_only_success_error_timeout      # R2 boundary

tests/test_session_paths.py
  - test_atomic_write_creates_temp_then_renames                 # R4 (1)
  - test_atomic_write_survives_interrupted_rename                # R4 (2)
  - test_canonical_file_mode_0o600_on_posix                     # R18 (1)
  - test_canonical_file_mode_best_effort_on_windows              # R18 platform guard
  - test_journal_dir_refuses_symlink_escape                      # proposal risk

tests/test_session_redaction.py
  - test_top_level_community_string_redacted                     # R10 (1)
  - test_nested_api_key_redacted_siblings_pass_through           # R10 (2)
  - test_redaction_runs_before_sanitization                      # R6 + R10 ordering

tests/test_session_rotation.py
  - test_rotate_displaces_oldest_step_to_ndjson                 # R5 (1)
  - test_ndjson_is_append_only_across_rotations                 # R5 (2)

tests/test_session_journal.py
  - test_first_tool_call_creates_canonical_file                 # R1 (1)
  - test_successful_tool_call_appends_one_step                  # R2 (1)
  - test_raising_tool_body_records_outcome_error                # R2 (2)
  - test_llm_interpretation_defaults_to_none                    # R2 (3)
  - test_persistence_completes_before_wrapper_returns           # R3 (1) [spy on os.replace]
  - test_mid_write_crash_leaves_parseable_file                 # R3 (2)
  - test_concurrent_writers_both_end_with_valid_json            # R4 (2)
  - test_private_ipv4_in_llm_interpretation_is_masked           # R6 (1)
  - test_resanitize_on_read_masks_bypass                        # R6 (2)
  - test_structured_fields_bypass_sanitizer                     # R6 (3)
  - test_get_state_idempotent_and_does_not_mutate               # R7 (1)
  - test_get_state_on_missing_file_creates_empty_state          # R7 (2)
  - test_set_focus_records_device_and_advances_last_updated     # R7 (3) + R14
  - test_set_focus_idempotent_on_same_device                    # R7 (4)
  - test_resume_loads_existing_session                          # R7 (5)
  - test_resume_raises_session_not_found_on_missing_file        # R7 (6)
  - test_session_id_immutable_across_many_writes                # R9 (1)
  - test_resume_does_not_mutate_previous_session                # R9 (2)
  - test_session_journal_package_has_no_network_imports        # R8 (1) [AST scan + runtime mocks]
  - test_corrupt_file_raises_journal_corrupt_error              # R11 (1)
  - test_next_write_auto_recovers_by_archiving_corrupt          # R11 (2)
  - test_health_soft_error_records_outcome_error_no_mutation    # R12
  - test_env_var_overrides_default_operator_alias               # R13
  - test_read_only_call_advances_last_updated                   # R14
  - test_disable_switch_skips_auto_trace                        # R15 (1)
  - test_disable_switch_makes_explicit_tools_raise              # R15 (2)
  - test_get_state_include_rotated_merges_ndjson_in_order       # R16
  - test_summarize_returns_markdown_with_required_fields        # R17

tests/test_session_journal_property.py
  - test_state_round_trip_under_arbitrary_mutation   # Hypothesis
  - test_trace_length_capped_after_many_appends      # R5 invariant
```

Coverage target: ≥ 85% for the new module (matches the Phase 1 baseline
pinned in the project-toolchain spec).

## 7. Work-unit commit plan

Per `work-unit-commits`: each commit compiles, each commit's tests pass,
each commit's diff < 400 LOC, each commit's message explains WHY not WHAT.
Twelve commits, in order:

1. **`chore(nora): add SessionJournal Settings fields + .env.example + redaction list`**
   - Touch: `src/nora/config.py` (+3 fields), `.env.example` (+4 lines),
     `src/nora/core/__init__.py` (skeleton), `src/nora/core/session_redaction.py`
     (`REDACTION_LIST` constant only, no walker).
   - Tests: `test_config.py` extended to cover the 3 new fields; new
     `test_session_redaction.py::test_frozen_list_is_a_frozenset`.
   - LOC: ~60.
   - Why: pin the env-var surface before any code reads it.

2. **`feat(nora/core): SessionState / SessionStep Pydantic models + typed exceptions`**
   - Touch: `src/nora/core/session_models.py`, `src/nora/core/session_journal.py`
     (exceptions only).
   - Tests: `test_session_models.py` (3 tests).
   - LOC: ~120.
   - Why: pure-data models first; no I/O, fast feedback.

3. **`feat(nora/core): atomic JSON write helper (temp + os.replace) with 0o600 mode`**
   - Touch: `src/nora/core/session_paths.py`.
   - Tests: `test_session_paths.py` (5 tests).
   - LOC: ~90.
   - Why: durability primitive; every later commit depends on it.

4. **`feat(nora/core): SessionJournal core (load / save / append / get_state) + tests`**
   - Touch: `src/nora/core/session_journal.py` (skeleton without rotation /
     redaction / sanitize).
   - Tests: `test_session_journal.py` (R1, R2, R3, R4, R9 happy path).
   - LOC: ~250.
   - Why: the central public API ships before any bells & whistles.

5. **`feat(nora/core): R10 redaction walker + Sanitizer integration on free-text fields`**
   - Touch: `src/nora/core/session_redaction.py` (walker), partial
     `session_journal.py` (call sites).
   - Tests: `test_session_redaction.py` (3 tests), extend
     `test_session_journal.py` (R6, R10 scenarios).
   - LOC: ~150.
   - Why: secrets-leak prevention is a non-negotiable, ships before the
     middleware starts writing to disk.

6. **`feat(nora/core): NDJSON rotation at max_trace_steps + append-only`**
   - Touch: `src/nora/core/session_rotation.py`, integrate in `record_step`.
   - Tests: `test_session_rotation.py` (2 tests), extend
     `test_session_journal.py` (R5 scenarios).
   - LOC: ~110.
   - Why: rotation is independent of redaction; isolate for clean rollback.

7. **`feat(nora/server): auto-trace FastMCP Middleware + integration with nora_health`**
   - Touch: `src/nora/server.py` (`_AutoTraceMiddleware`, `_journal`,
     `init_session_journal`, `mcp.add_middleware(...)` call in
     `__main__.py`).
   - Tests: new `tests/test_auto_trace_middleware.py` with an in-process
     FastMCP `Client` driving `nora_health`; asserts one step in the
     trace; asserts the 4-tuple response is byte-identical.
   - LOC: ~200.
   - Why: the middleware is the most intricate piece; ship it as one
     reviewable unit so a reviewer can sign off on the recording contract
     in isolation.

8. **`feat(nora/server): three explicit recall tools (get_state / set_focus / resume)`**
   - Touch: `src/nora/server.py` (3 `@mcp.tool`).
   - Tests: `tests/test_session_recall_tools.py` driving the tools
     through FastMCP `Client`; assert R7 scenarios.
   - LOC: ~150.
   - Why: the explicit recall surface ships after auto-trace so the
     middleware is the obvious single source of "what got recorded".

9. **`feat(nora/core): nora_session_summarize() Markdown projection (R17)`**
   - Touch: `src/nora/core/session_journal.py::summarize`,
     `src/nora/server.py` (`@mcp.tool`).
   - Tests: R17 scenario.
   - LOC: ~80.
   - Why: convenience projection; orthogonal to the durable record.

10. **`feat(nora/core): NORA_SESSION_JOURNAL_ENABLED disable switch (R15)`**
    - Touch: `src/nora/core/session_journal.py` (gating in `record_step`,
      `set_focus`, `resume`, `get_state`, `summarize`).
    - Tests: R15 scenarios (2 tests) + an integration test that asserts
      the disable switch leaves `nora_health` 4-tuple unchanged.
    - LOC: ~50.
    - Why: opt-out is a one-line guard at each public entry point; cheap
      to add, expensive to retrofit later.

11. **`test(nora): R11 corrupt-file auto-recovery + R18 POSIX 0o600 + cross-platform mode`**
    - Touch: `tests/test_session_journal.py` (extend with R11, R18, R9
      session-id-immutable scenarios not yet covered).
    - LOC: ~120.
    - Why: failure-mode coverage; only after the happy path is solid.

12. **`test(nora): Hypothesis property tests + air-gap static scan (R8)`**
    - Touch: `tests/test_session_journal_property.py`,
      `tests/test_session_journal.py::test_session_journal_package_has_no_network_imports`.
    - Dev dep: add `hypothesis` to `[dependency-groups].dev`.
    - LOC: ~120.
    - Why: invariants + air-gap are the last safety net before archive;
      cheap to add and they pin the design's two hardest properties.

Total authored additions across 12 commits: ~1500 LOC (well under the
8k-LOC chained-PR ceiling). Per commit: ≤ 250 LOC. A reviewer can read
each commit end-to-end.

## 8. Risks addressed by the design

| Risk (from proposal) | Design decision | Residual risk |
|---|---|---|
| Mid-write crash leaves torn JSON | `atomic_write_json` (temp + `os.replace`); R3 ordering puts persistence BEFORE wrapper return; orphan `*.json.tmp` is ignored on next open. | Disk-full exhaustion still aborts; we log WARNING and the tool body raises (no silent loss). |
| Path injection via `nora_session_journal_dir` | `ensure_journal_dir` resolves to absolute, `mkdir(0o700)`, and refuses symlinks that point outside the resolved tree; validated once at boot, cached in the `SessionJournal` instance. | Operator can still pre-create a symlink before boot pointing inside the tree; we accept that since `0o700` on the resolved dir confines the blast radius. |
| Auto-trace I/O stalls the MCP loop | One `os.replace` per call (≈ 1 fsync); no extra in-process lock; rotation is amortized (one rename per threshold). | Sustained ≥ 100 calls/sec on slow disks is untested; the proposal's p95 < 10 ms target is asserted in `test_record_step_p95_under_10ms_on_tmpfs`. |
| Free-text sanitization gap | Sanitizer is invoked on every free-text field at write AND read (R6); one `Sanitizer` instance per `SessionJournal` keeps aliases stable within a session. | Across server restarts, aliases change (the `Sanitizer` re-instantiates). R6's "same `Sanitizer` instance" wording is honored; cross-restart continuity is not in the contract. |
| JSON growth past rotation policy | `rotate_if_needed` displaces one step per threshold overflow; NDJSON is append-only; no auto-truncation. | Operator disk budget is the only ceiling; no proactive compaction. R19 gzip-after-24h is MAY and deferred to a follow-up change. |

## 9. Open questions for the operator

1. **R10 frozen redaction list — discrepancy between spec text and preflight.**
   The spec (R10) freezes: `community, community_string, auth_password,
   auth_key, priv_password, priv_key, password, ssh_password, api_key,
   token, secret` (11 keys). The preflight instruction lists 12 keys
   including `snmp_community, passwd, apikey, private_key` and omitting
   the four `*_password` ones. The spec is the testable contract; should
   the design follow the spec list verbatim, or is the preflight the
   intended update? If the latter, the spec needs a one-line patch
   before the Phase 2 archive.

2. **R19 / R20 / R21 — out of scope for this change?** The spec marks
   them `MAY`. The proposal explicitly says "out of scope" and the
   design follows that. Confirm: a follow-up change is acceptable, no
   need to ship hooks in this PR.
