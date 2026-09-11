# Tasks: SessionJournal — Cross-Tool-Call LLM Memory (Phase 2 — Foundation)

> Sibling to `phase2-pmp450i-driver`; this change ships first. Spec: `openspec/changes/phase2-session-journal/specs/session-journal/spec.md` (21 requirements, 37 scenarios). Design: `openspec/changes/phase2-session-journal/design.md` §7 defines 12 commits that drive these tasks. Strict TDD: RED → GREEN → REFACTOR per sub-task. Review budget: 800 lines / 400 LOC per PR slice.

## Test layout

```
tests/
  core/
    __init__.py
    conftest.py                # journal_dir (tmp_path), settings (tmp_path), fresh_sanitizer
    test_session_models.py     # R1, R2, R9 model-only (no I/O)
    test_session_paths.py      # R3, R4, R18 + symlink guard
    test_session_redaction.py  # R6 ordering, R10 walker + frozen list
    test_session_rotation.py   # R5 NDJSON displacement
    test_session_journal.py    # R1–R4, R6, R7-S2, R9, R11, R13, R14, R15, R16, R18
    test_session_journal_property.py  # Hypothesis round-trip + R5 invariant
  test_server_auto_trace.py    # R2, R3 ordering, R12 — middleware wraps nora_health (FastMCP Client)
  test_server_session_tools.py # R7, R15, R16, R17 — 4 explicit tools (FastMCP Client)
  test_session_journal_airgap.py    # R8 — AST + runtime scan for banned imports
```

Naming: `test_<unit>_<behaviour>` (matches `tests/test_server.py`, `tests/test_config.py`). Fixtures: `journal_dir(tmp_path) -> Path`, `settings(tmp_path) -> Settings`, `fresh_sanitizer() -> Sanitizer`.

## 1. Settings fields + `.env.example` + R10 frozen redaction list — design commit #1

Pins the env-var surface (R10 frozen list, R13, R15 partial env, secure-configuration delta). Everything downstream reads `Settings.nora_session_journal_dir`, `nora_session_trace_max_steps`, `nora_session_journal_enabled`, `nora_operator_alias`. The frozen list ships now so the walker (commit #5) only adds behaviour, not contract.

- [x] 1.1 RED - extend `tests/test_config.py` asserting 4 new `Settings` fields with defaults (`./var/sessions/`, `50`, `true`, `"anonymous"`); assert `.env.example` lists each with sanitized placeholders (no real paths/IPs/creds); assert `nora.core.session_redaction.REDACTION_LIST` is `frozenset[str]` with the 11 R10 keys verbatim.
- [x] 1.2 GREEN - add 4 fields to `src/nora/config.py`; append sanitized keys to `.env.example`; create `src/nora/core/__init__.py` (empty package) + `src/nora/core/session_redaction.py` exporting `REDACTION_LIST: Final[frozenset[str]] = frozenset({"community","community_string","auth_password","auth_key","priv_password","priv_key","password","ssh_password","api_key","token","secret"})` and `_REDACTION_MARKER = "[REDACTED]"` (used by commit #5).
- [x] 1.3 REFACTOR - extract `_DEFAULT_OPERATOR_ALIAS = "anonymous"` constant in `config.py`; confirm `pytest tests/test_config.py -q` + `ruff check src/nora/config.py src/nora/core/` + `mypy --strict src/nora` exit 0.

Depends on: none. Aligned with design §7 commit 1. Covers scenarios: R10 list (precondition), R13-S1, R15 env surface.

## 2. SessionState / SessionStep models + typed exceptions — design commit #2

Pure data layer + typed exception hierarchy (R1, R2 core types; R7, R11, R15 exception names). No I/O; Pydantic validation alone. Pinned before journal core so failures surface as typed errors, not `KeyError`/`dict` chaos.

- [x] 2.1 RED - failing `tests/core/test_session_models.py` covering R1 field set (7 keys, `focus_device_id is None`, `trace == []`, `devices_reviewed == []`), R2 7-field `SessionStep` (`ts`/`step`/`tool`/`input`/`result_summary`/`duration_ms`/`outcome`), R2 `Outcome` literal constraint (rejects `"warn"`), R9 `session_id` initial state, R13 `operator_alias` `max_length=64` (rejects 65 chars).
- [x] 2.2 GREEN - implement `src/nora/core/session_models.py` per design §2 (`SessionStep`, `SessionState`, `Outcome = Literal["success","error","timeout"]`); add exception hierarchy in `src/nora/core/session_journal.py` skeleton: `SessionJournalError(Exception)`, `SessionNotFoundError`, `JournalCorruptError`, `JournalDisabledError` — bodies land in later commits.
- [x] 2.3 REFACTOR - extract `Outcome` to module-level alias; add `__all__` to `session_models.py`; confirm `pytest tests/core/test_session_models.py -q` + `mypy --strict src/nora/core` exit 0.

Depends on: 1. Aligned with design §7 commit 2. Covers scenarios: R1 (model only), R2 (model only), R9 (model only).

## 3. Atomic JSON write helper (temp + `os.replace`) with 0o600 mode — design commit #3

Durability primitive (R3 ordering, R4 atomicity, R18 0o600). Every later commit depends on this. POSIX 0o600 is best-effort on non-POSIX per R18 platform guard.

- [x] 3.1 RED - failing `tests/core/test_session_paths.py`: (a) `atomic_write_json` creates `*.tmp` then renames; (b) interrupted rename leaves parseable file + orphan `*.tmp`; (c) `ensure_journal_dir` creates dir at `0o700`; (d) `ensure_journal_dir` refuses symlink escape (tmp_path symlink pointing outside); (e) `canonical_path` / `ndjson_path` resolve under `journal_dir`.
- [x] 3.2 GREEN - implement `src/nora/core/session_paths.py`: `atomic_write_json(path, payload)` (write to `path.with_suffix(path.suffix + ".tmp")` → `os.replace`, then `os.chmod(0o600)` best-effort); `ensure_journal_dir(path)` (`resolve(strict=False)`, `mkdir(parents=True, exist_ok=True, mode=0o700)`, refuse if any ancestor is a symlink whose `resolve()` lands outside the journal tree); `canonical_path(journal_dir, session_id)` and `ndjson_path(journal_dir, session_id)` helpers.
- [x] 3.3 REFACTOR - extract `_POSIX = os.name == "posix"` guard; mark Windows-only assertions with `pytest.mark.skipif(sys.platform == "win32", ...)`; add docstrings naming R3/R4/R18; confirm `pytest tests/core/test_session_paths.py -q` exits 0.

Depends on: 1. Aligned with design §7 commit 3. Covers scenarios: R3-S2, R4-S1, R4-S2, R18-S1, proposal risk "path injection".

## 4. SessionJournal core (load / save / append / get_state) — design commit #4

Central public API happy path (R1, R2, R3 ordering, R9 immutability, R7-S2 missing-file). Ships before redaction/rotation so a reviewer signs the recording contract in isolation. Sanitizer/redaction wired as TODO no-op returning input unchanged this commit.

- [x] 4.1 RED - failing `tests/core/test_session_journal.py`: (a) R1-S1 first tool call creates canonical file with all 7 keys + parses as UUIDv4; (b) R2-S1 successful call appends one step with `step==1`, `outcome=="success"`; (c) R2-S3 `llm_interpretation is None`; (d) R3-S1 persistence fires before wrapper returns (spy on `atomic_write_json`); (e) R9-S1 session_id stable across 5 writes; (f) R7-S2 `_load_or_create` on missing file creates empty state.
- [x] 4.2 GREEN - implement `SessionJournal.__init__`, `_load_or_create`, `_persist`, `record_step` (R10/Sanitizer calls wired as TODO no-op this commit), `get_state` (without `include_rotated` yet), module-level `_journal: SessionJournal | None = None`. Add `SessionJournal.for_settings(settings, sanitizer)` factory. Use exceptions from task #2.
- [x] 4.3 REFACTOR - extract `_next_step(state) -> int` helper; add `__all__` to `session_journal.py`; confirm `pytest tests/core/test_session_journal.py -q` exits 0 + coverage for `session_journal.py` ≥ 85%.

Depends on: 1, 2, 3. Aligned with design §7 commit 4. Covers scenarios: R1-S1, R2-S1, R2-S3, R3-S1, R7-S2, R9-S1.

## 5. R10 redaction walker + Sanitizer integration — design commit #5

Secret-leak prevention (R6 write+read sanitization, R10 name-keyed redaction). Runs BEFORE sanitization so `_REDACTION_MARKER` is never re-aliased. Walker is a recursive pure function — no Pydantic coupling — so it also runs on untrusted payloads before `SessionStep` is built.

- [x] 5.1 RED - failing `tests/core/test_session_redaction.py`: (a) R10-S1 top-level `community` redacted to `_REDACTION_MARKER`; (b) R10-S2 nested `api_key` redacted, siblings pass through; (c) R6 ordering — redaction runs before sanitization (`_REDACTION_MARKER` survives a sanitize pass). Extend `test_session_journal.py` with R6-S1 (private IPv4 in `llm_interpretation` masked on write → alias appears on disk; `10.0.0.5` literal absent) and R6-S3 (structured fields `tool`/`step`/`duration_ms` byte-identical round-trip).
- [x] 5.2 GREEN - implement `redact(value: Any) -> Any` in `src/nora/core/session_redaction.py` (recursive walker: `dict` → check key against `REDACTION_LIST`, replace value with `_REDACTION_MARKER`; `list` → map; scalars → passthrough). Wire `_sanitize_tree(redacted, self._sanitizer)` + `self._sanitizer.sanitize(result_summary).text` + `self._sanitizer.sanitize(llm_interpretation).text` inside `SessionJournal.record_step`.
- [x] 5.3 REFACTOR - add re-sanitization on read in `_load_or_create` for `result_summary` + free-text `input` string fields (R6-S2 defense in depth); confirm full `test_session_redaction.py` + `test_session_journal.py` green and round-trip-mask test passes.

Depends on: 4. Aligned with design §7 commit 5. Covers scenarios: R6-S1, R6-S3, R10-S1, R10-S2.

## 6. NDJSON rotation at `nora_session_trace_max_steps` — design commit #6

Rotation policy (R5). Append-only NDJSON; canonical `trace` stays bounded. Isolated module so its tests don't drag the whole journal into scope.

- [x] 6.1 RED - failing `tests/core/test_session_rotation.py`: (a) R5-S1 threshold overflow displaces oldest step to NDJSON with `step==1`; (b) R5-S2 two consecutive rotations append both displaced steps in displacement order. Extend `test_session_journal.py` with `test_record_step_rotates_at_threshold` driving the boundary through `SessionJournal`.
- [x] 6.2 GREEN - implement `src/nora/core/session_rotation.py::rotate_if_needed(state, ndjson_path, max_steps)` returning a new `SessionState` with the oldest step popped and appended to NDJSON (one JSON object per line via `Path.open("a")`). Wire into `SessionJournal.record_step` between append and persist.
- [x] 6.3 REFACTOR - extract `_NDJSON_SUFFIX = ".log.ndjson"` constant; chmod NDJSON to `0o600` on POSIX best-effort; confirm `pytest tests/core/test_session_rotation.py tests/core/test_session_journal.py -q` exit 0.

Depends on: 4, 5. Aligned with design §7 commit 6. Covers scenarios: R5-S1, R5-S2.

## 7. FastMCP auto-trace middleware + integration with `nora_health` — design commit #7

The recording contract (R2 middleware, R3 write-then-return, R12 byte-identical 4-tuple). FastMCP 3.2+ `Middleware` base class; `on_call_tool(context, call_next)` wraps every `@mcp.tool` invocation.

- [x] 7.1 RED - failing `tests/test_server_auto_trace.py`: (a) invoking `nora_health` via in-process FastMCP `Client` produces exactly one journal step with `tool=="nora_health"`, `outcome=="success"`; (b) `nora_health` 4-tuple response is byte-identical (`{"version","active_provider","connectivity","env_loaded"}`); (c) tool body that raises records `outcome=="error"` and re-raises without swallowing (R12 invariant).
- [x] 7.2 GREEN - add `_AutoTraceMiddleware(fastmcp.Middleware)` to `src/nora/server.py` implementing `on_call_tool`: capture `ts = datetime.now(timezone.utc)` BEFORE, await `call_next`, on `Exception` record + re-raise, on success record; record via `journal.record_step(...)`. Wire `_journal` module-level singleton + `init_session_journal(settings, journal)` (accepts injected journal for tests); register via `mcp.add_middleware(_AutoTraceMiddleware(get_journal()))` in `src/nora/__main__.py` boot AFTER `set_runtime_state`.
- [x] 7.3 REFACTOR - extract `_derive_summary(result)` helper (for non-string results: `repr(result)[:256]`); add `logger.info("auto-trace tool=%s duration_ms=%d outcome=%s", ...)`; confirm `pytest tests/test_server_auto_trace.py -q` + full existing `test_server.py` green; assert 4-tuple preserved.

Depends on: 4, 5. Aligned with design §7 commit 7. Covers scenarios: R2-S1, R2-S2, R3-S1, R12.

## 8. Three explicit recall tools (`get_state` / `set_focus` / `resume`) — design commit #8

Operator + LLM-facing surface (R7, R9-S2, R14-S1). Ships AFTER auto-trace so the middleware is the single source of "what got recorded".

- [x] 8.1 RED - failing `tests/test_server_session_tools.py`: (a) R7-S1 `nora_session_get_state` twice returns equivalent state, neither mutates; (b) R7-S2 missing file → empty state + canonical created; (c) R7-S3 `nora_session_set_focus("ap-7400-01")` records + appends to `devices_reviewed` + advances `last_updated` (R14); (d) R7-S4 idempotent on same device; (e) R7-S5 `nora_session_resume` loads existing session + subsequent writes go to resumed file; (f) R7-S6 `resume` on missing file raises `SessionNotFoundError`, active session untouched; (g) R9-S2 `resume` doesn't mutate previous session; (h) R16-S1 `get_state(include_rotated=True)` merges NDJSON tail in `step` order.
- [x] 8.2 GREEN - register three `@mcp.tool` in `src/nora/server.py`: `nora_session_get_state(include_rotated: bool = False) -> dict[str, Any]` (serialises `SessionState` via `.model_dump(mode="json")`), `nora_session_set_focus(device_id: str) -> dict[str, Any]`, `nora_session_resume(session_id: str) -> dict[str, Any]` (raises `SessionNotFoundError` on missing file). All three route through `get_journal()`. Implement `SessionJournal._rehydrate_rotated` for R16.
- [x] 8.3 REFACTOR - extract `_state_to_payload(state: SessionState) -> dict[str, Any]` helper; add `mcp.tool` docstrings referencing R7 scenarios; confirm `pytest tests/test_server_session_tools.py -q` green.

Depends on: 7. Aligned with design §7 commit 8. Covers scenarios: R7-S1..S6, R9-S2, R14-S1, R16-S1.

## 9. `nora_session_summarize()` Markdown projection — design commit #9

Operator-facing digest (R17). Last 10 step summaries + `focus_device_id` + `devices_reviewed`. Pure projection over `get_state()`; orthogonal to the durable record.

- [x] 9.1 RED - failing `tests/core/test_session_summarize.py` + integration test: R17 scenario — `nora_session_summarize()` returns non-empty Markdown containing `focus_device_id`, both device IDs from `devices_reviewed`, and at least one tool name from the trace.
- [x] 9.2 GREEN - implement `SessionJournal.summarize(self) -> str` (Markdown template: `# Session {session_id}`, `**Focus**: {focus}`, `**Devices reviewed**: {list}`, `## Recent steps (last 10)` table). Register `@mcp.tool nora_session_summarize() -> str` in `src/nora/server.py`.
- [x] 9.3 REFACTOR - truncate step `result_summary` to 120 chars in the table; confirm `pytest tests/core/test_session_summarize.py tests/test_server_session_tools.py -k summarize -q` green.

Depends on: 4. Aligned with design §7 commit 9. Covers scenarios: R17.

## 10. `NORA_SESSION_JOURNAL_ENABLED` disable switch — design commit #10

Operator-controlled opt-out (R15). One guard at each public entry point. Must NOT alter `nora_health` 4-tuple response.

- [x] 10.1 RED - failing extension to `test_server_auto_trace.py` + `test_server_session_tools.py`: (a) R15-S1 `NORA_SESSION_JOURNAL_ENABLED=="false"` → no file under journal dir after tool call + `nora_health` response byte-identical to enabled-state; (b) R15-S2 same env → `nora_session_get_state()` raises `JournalDisabledError`; (c) same for `set_focus` / `resume` / `summarize`.
- [x] 10.2 GREEN - gate in `SessionJournal.record_step` (return without persisting when disabled), `get_state` / `set_focus` / `resume` / `summarize` (raise `JournalDisabledError`). Wire `Settings.nora_session_journal_enabled` (default `True`) into `SessionJournal.for_settings`.
- [x] 10.3 REFACTOR - extract `_DISABLED_MSG = "session journal disabled via NORA_SESSION_JOURNAL_ENABLED"` constant; confirm full `test_server.py` + new tests green; assert `nora_health` 4-tuple preserved across both enabled/disabled states.

Depends on: 7, 8, 9. Aligned with design §7 commit 10. Covers scenarios: R15-S1, R15-S2.

## 11. R11 corrupt-file auto-recovery + R18 POSIX 0o600 + cross-platform mode — design commit #11

Failure-mode coverage (R11 explicit scenarios, R18 end-to-end assertion). Only after happy path is solid.

- [x] 11.1 RED - failing extensions to `tests/core/test_session_journal.py`: (a) R11-S1 reading bytes-that-aren't-JSON raises `JournalCorruptError`, MCP server alive (asserted via follow-up `nora_session_get_state` call); (b) R11-S2 next write auto-recovers by renaming to `${session_id}.corrupt-{unix_ts}.json` and writing fresh canonical; (c) R18 end-to-end POSIX `0o600` assertion after `SessionJournal._persist`; (d) cross-platform mode test asserting non-POSIX skip path is non-erroring.
- [x] 11.2 GREEN - implement `SessionJournal._recover_from_corrupt(path)`: rename to `path.with_suffix(f".corrupt-{int(time.time())}.json")`, log WARNING, leave fresh canonical creation to next `_load_or_create`. Wrap `json.loads` in `_load_or_create` with try/except → `JournalCorruptError(path)`. Confirm atomic write's `os.chmod(0o600)` is a no-op on Windows (already guarded by `_POSIX` from task #3).
- [x] 11.3 REFACTOR - extract `_CORRUPT_SUFFIX_TEMPLATE = ".corrupt-{ts}.json"`; confirm `pytest tests/core/test_session_journal.py -k 'corrupt or posix' -q` green on darwin.

Depends on: 4, 5. Aligned with design §7 commit 11. Covers scenarios: R11-S1, R11-S2, R18-S1.

## 12. Hypothesis property tests + air-gap static scan (R8) — design commit #12

Invariants + air-gap safety net. Last safety net before archive.

- [x] 12.1 RED - failing `tests/core/test_session_journal_property.py`: (a) `state == load(save(state))` under arbitrary mutation sequences (Hypothesis `@given` strategies for `SessionStep` lists); (b) trace length capped after many appends even with rotation disabled (R5 invariant). Plus `tests/test_session_journal_airgap.py`: (c) AST scan over `src/nora/core/*.py` for `requests|httpx|urllib.request|socket|ssl|http.client` → zero matches; (d) runtime test mocking `socket.socket`, `urllib.request.urlopen`, `httpx.get` and asserting none are called when the full middleware path runs.
- [x] 12.2 GREEN - implement Hypothesis strategies (`st.lists(session_steps, max_size=100)`), add `hypothesis` to `[dependency-groups].dev` in `pyproject.toml`. Implement `_scan_banned_imports()` AST helper + the runtime mock-based test.
- [x] 12.3 REFACTOR - extract `_BANNED_IMPORT_PATTERNS = (...)` constant; register `pytest.mark.slow` marker; confirm `pytest -m slow tests/core/test_session_journal_property.py tests/test_session_journal_airgap.py -q` green.

Depends on: 4, 5, 6, 11. Aligned with design §7 commit 12. Covers scenarios: R8-S1, R5 invariant, R6 round-trip.

## 13. Verify — runs full suite + produces verify-report evidence

Pre-archive gate. Standard verify task per SDD `sdd-verify` skill: run pytest + coverage + ruff + mypy, capture evidence in `verify-report.md`. NOT a TDD task — pure evidence collection.

- [ ] 13.1 RUN - `uv sync` (reproduce env); `uv run pytest --cov=src/nora --cov-report=term-missing -q` exits 0 with coverage ≥ 85% (per project-toolchain spec); `uv run ruff check .` exits 0; `uv run ruff format --check .` exits 0; `uv run mypy --strict src/nora` exits 0.
- [ ] 13.2 RUN - air-gap audit: `grep -rE 'requests|httpx|urllib\.request|^import socket|^import ssl|^import http\.client' src/nora/core/` returns zero matches.
- [ ] 13.3 WRITE - produce `openspec/changes/phase2-session-journal/verify-report.md` with: pytest summary line, coverage table for new module, ruff/mypy output, air-gap grep output, scenario-to-test mapping table (R1..R18 with `tests/test_*.py::test_*` references), `git diff --stat` total.

Depends on: 1–12 complete. Aligned with SDD `sdd-verify` skill.

## 14. Archive — sync delta spec into `openspec/specs/session-journal/spec.md`

Final SDD step. Per `sdd-archive` skill: copy delta spec to archived location; produce archive report. NOT a TDD task.

- [ ] 14.1 COPY - `mkdir -p openspec/specs/session-journal`; copy `openspec/changes/phase2-session-journal/specs/session-journal/spec.md` → `openspec/specs/session-journal/spec.md` (no edits — spec is the authoritative source).
- [ ] 14.2 WRITE - produce `openspec/changes/phase2-session-journal/archive-report.md` per archive convention: change title, scope summary, scenarios archived (37), source files affected, link to `verify-report.md`, link to committed PR(s).
- [ ] 14.3 VERIFY - `ls openspec/specs/session-journal/spec.md` exists; `git status` shows the change directory clean of uncommitted edits; confirm `git log` shows all 12 design commits on the active branch.

Depends on: 13 (verify passes). Aligned with SDD `sdd-archive` skill.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~1500 (12 design commits × ~125 LOC authored + tests in adjacent files) |
| 800-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | 12 commit-aligned work units (per design §7); each ≤ 250 LOC authored |
| Delivery strategy | ask-on-risk |
| Chain strategy | stacked-to-main |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
800-line budget risk: High

Rationale: design §7 sums to ~1500 LOC authored additions across 12 commits (above the 800-line review budget). Each commit is independently testable, revertable, and ≤ 250 LOC. `delivery_strategy: ask-on-risk` → orchestrator should surface the chain strategy (stacked-to-main vs feature-branch-chain vs size-exception) to the operator before `sdd-apply` starts. **stacked-to-main** is the natural fit for 12 small orthogonal slices that each merge independently and have no shared blast radius; feature-branch-chain would add overhead without rollback benefit at this granularity; size-exception is rejected because the per-commit slices are already small.
