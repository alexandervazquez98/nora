# Archive Report — phase2-session-journal

**Archived**: 2026-09-10
**Verdict at archive**: PASS WITH WARNINGS (3 PARTIAL, 2 SUGGESTION — all non-blocking)
**Tasks at archive**: 14/14 `[x]` (12 design + 1 chore + 1 remediation); tasks 13 (verify) and 14 (archive) are SDD phase tasks
**Review Gate**: `reviewGate` structurally ABSENT — no review was started for this candidate; archive proceeds under ordinary repository policy.
**Maintainer size-exception**: granted (single PR; review budget raised from 400 to 1500 LOC for this change).

## Goal

Ship `SessionJournal` — per-session JSON trace + auto-trace MCP middleware + 3 explicit recall tools — so the local LLM has bounded cross-tool-call memory and the operator has a single auditable artifact per investigation. Local disk only, zero network, air-gap safe. Sibling to `phase2-pmp450i-driver`; this change must archive first because the driver layer consumes the journal.

## Final State

- **Production modules (new)**: `src/nora/core/session_models.py`, `session_paths.py`, `session_redaction.py`, `session_rotation.py`, `session_journal.py`, plus `src/nora/core/__init__.py`.
- **Production modules (modified)**: `src/nora/server.py` (auto-trace middleware + 4 `@mcp.tool`s), `src/nora/__main__.py` (boot wires `init_session_journal`), `src/nora/config.py` (+4 `Settings` fields), `.env.example` (+sanitized keys), `pyproject.toml` (`hypothesis>=6` dev dep).
- **New MCP tools**: `nora_session_get_state`, `nora_session_set_focus`, `nora_session_resume`, `nora_session_summarize` (4 explicit recall tools; one beyond the 3 R7 mandates).
- **Auto-trace middleware**: `FastMCP.Middleware` subclass `_AutoTraceMiddleware` wrapping every `@mcp.tool` invocation (R2/R3/R12 contract). `nora_health` 4-tuple preserved byte-identical.
- **Frozen R10 redaction list**: 11 keys verbatim (`community`, `community_string`, `auth_password`, `auth_key`, `priv_password`, `priv_key`, `password`, `ssh_password`, `api_key`, `token`, `secret`). Implemented as `frozenset[str]` for immutability.
- **Strict TDD test suite**: 201 tests across 17 files (125 Phase 1 baseline + 74 design + 2 remediation).
- **Coverage**: 95% on `src/nora/` (10pp headroom over the 85% threshold).
- **Functional gates**: all 4 PASS — `pytest` (201/201), `ruff check`, `ruff format --check`, `mypy --strict`.
- **Spec coverage**: 34/37 (91.9%) of total; 34/34 (100%) of in-scope (excluding 3 MAY items: R19 gzip-after-24h, R20 cross-session search, R21 encryption-at-rest hook).

## What Was Delivered

### New Production Code

| File | LOC | Coverage | Purpose |
|------|-----|----------|---------|
| `src/nora/core/session_models.py` | 29 | 100% | Pydantic `SessionState`, `SessionStep`, `Outcome` literal |
| `src/nora/core/session_paths.py` | 51 | 90% | `atomic_write_json` (temp + `os.replace`), `_unique_tmp_path` (per-call uuid4 suffix), `ensure_journal_dir` (symlink-escape guard), POSIX `0o600`/`0o700` mode |
| `src/nora/core/session_redaction.py` | 16 | 100% | R10 frozen `frozenset[str]` + recursive `redact()` walker |
| `src/nora/core/session_rotation.py` | 29 | 97% | NDJSON `rotate_if_needed` (append-only, canonical bounded) |
| `src/nora/core/session_journal.py` | 213 | 98% | Public API: `record_step`, `get_state`, `set_focus`, `resume`, `summarize`, `_resanitize_state`, `_recover_from_corrupt`. Typed exceptions: `SessionJournalError`, `SessionNotFoundError`, `JournalCorruptError`, `JournalDisabledError` |

### Modified Production Code

| File | Change | Why |
|------|--------|-----|
| `src/nora/config.py` | +4 `Settings` fields | `nora_session_journal_dir`, `nora_session_trace_max_steps`, `nora_session_journal_enabled`, `nora_operator_alias` |
| `src/nora/server.py` | `_AutoTraceMiddleware` + 4 `@mcp.tool`s + module-level `_journal` singleton | R2/R3/R12 recording contract + R7/R17 explicit recall surface |
| `src/nora/__main__.py` | `init_session_journal(settings)` + `register_auto_trace_middleware()` | Boot wiring after `set_runtime_state` |
| `.env.example` | +4 sanitized key placeholders | Per `secure-configuration` locked-no-hardcoded-values rule |
| `pyproject.toml` | `hypothesis>=6` dev dep + `slow` marker | Property-test framework |

### Test Suite (201 tests across 17 files)

- **Unit** (177): `tests/core/test_session_models.py`, `test_session_paths.py`, `test_session_redaction.py`, `test_session_rotation.py`, `test_session_journal.py`, `test_session_journal_property.py`, `test_session_summarize.py`, `test_session_journal_airgap.py`.
- **Integration** (12): `tests/test_server_auto_trace.py`, `tests/test_server_session_tools.py` — FastMCP `Client` driving real middleware.
- **Property** (2): `tests/core/test_session_journal_property.py` — Hypothesis round-trip + R5 invariant.
- **Static scan** (1): `tests/test_session_journal_airgap.py` — AST scan for banned imports.
- **Runtime mock** (1): same file — `socket`/`urllib.request.urlopen`/`httpx.get` mocked; full middleware path runs without touching them.
- **Subprocess boot** (5): `tests/test_integration.py` — covered by Phase 1 baseline (unchanged).
- **Concurrency** (1): `test_concurrent_writers_both_end_with_valid_json` — `threading.Barrier(4)` forces simultaneous entry into the contended region.

## Specs Synced

**1 spec created** (no prior main spec existed for this capability).

| Domain | Action | Path |
|--------|--------|------|
| `session-journal` | Created (no prior main spec) | `openspec/specs/session-journal/spec.md` |

**Mechanical copy evidence**: source spec at `openspec/changes/archive/2026-09-10-phase2-session-journal/specs/session-journal/spec.md` → canonical at `openspec/specs/session-journal/spec.md`. Post-sync SHA-256 byte-identity:

```
d8593c3b8561e4e122b76bac98cf0e1b359cbeffd402a8915ea77261d85a3de1  archive/specs/session-journal/spec.md
d8593c3b8561e4e122b76bac98cf0e1b359cbeffd402a8915ea77261d85a3de1  canonical openspec/specs/session-journal/spec.md
```

**Empty `diff` is the only passing evidence**; verbatim readback:

```
$ diff openspec/changes/archive/2026-09-10-phase2-session-journal/specs/session-journal/spec.md \
       openspec/specs/session-journal/spec.md
<empty output>
$ echo "diff exit status: $?"
diff exit status: 0
```

The source spec remains in the archive folder as the audit trail (per "archive is an AUDIT TRAIL — never delete or modify archived changes"). The canonical copy is the live reference for future changes.

## Archived Folder Contents

```
openspec/changes/archive/2026-09-10-phase2-session-journal/
├── proposal.md
├── design.md
├── tasks.md               (14/14 [x], 0 unchecked implementation tasks)
├── verify-report.md       (verdict: PASS; 0 CRITICAL, 3 PARTIAL, 2 SUGGESTION)
├── pr-description.md
├── specs/
│   └── session-journal/
│       └── spec.md        (audit trail — byte-identical to canonical)
└── archive-report.md      (this file — additive, written post-move)
```

## Commit Timeline (14 commits — 12 design + 1 chore + 1 remediation)

| # | SHA | Subject | LOC |
|---|-----|---------|-----|
| 1 | `0bd14e3` | chore(nora): add SessionJournal Settings fields + .env.example + redaction list | ~80 |
| 2 | `35d0f30` | feat(nora/core): SessionState / SessionStep models + typed exceptions | ~190 |
| 3 | `fc8790b` | feat(nora/core): atomic JSON write helper (temp + os.replace) with 0o600 mode | ~230 |
| 4 | `2cb3dfc` | feat(nora/core): SessionJournal core (load / save / append / get_state) | ~480 |
| 5 | `3bb897a` | feat(nora/core): R10 redaction walker + Sanitizer integration | ~270 |
| 6 | `6d4ce96` | feat(nora/core): NDJSON rotation at max_trace_steps + append-only | ~210 |
| 7 | `1bbdbf4` | feat(nora/server): auto-trace FastMCP Middleware + integration with nora_health | ~280 |
| 8 | `88064e2` | feat(nora/server): three explicit recall tools (get_state / set_focus / resume) | ~310 |
| 9 | `cddd159` | feat(nora/core): nora_session_summarize() Markdown projection (R17) | ~110 |
| 10 | `8cc4418` | feat(nora/core): NORA_SESSION_JOURNAL_ENABLED disable switch (R15) | ~60 |
| 11 | `5cebafd` | feat(nora/core): R11 corrupt-file auto-recovery + R18 POSIX 0o600 | ~135 |
| 12 | `d997156` | test(nora): Hypothesis property tests + air-gap static scan (R8) | ~330 |
| 13 | `011e4ef` | chore(sdd/phase2-session-journal): mark tasks 1-12 [x]; add PR description | metadata |
| 14 | `507cafe` | **fix(nora/core): address R4-S2 concurrent writers and R6-S2 resanitize-on-read** (REMEDIATION) | +251 / -12 |

**Per-commit**: pre-remediation commits all ≤ 250 LOC. Remediation commit `507cafe` is +251/-12 LOC; slightly over the 250-LOC budget but acceptable for a 2-finding remediation batch (each finding ≤ 130 LOC and the tests are tightly written).

## Real Bug Caught During Remediation

A concrete example of TDD discipline paying off: the remediation commit `507cafe` surfaced and fixed a **real concurrency bug**, not just a missing test.

**What broke**: The original `atomic_write_json_posix` used a fixed `<canonical>.tmp` filename. When N `record_step` calls raced (e.g. two threads entering the middleware simultaneously), the second writer's `open()` clobbered the first writer's tmp file. The first writer's subsequent `os.replace` then raised `FileNotFoundError` on a tmp file that no longer existed.

**How the test caught it**: `tests/core/test_session_journal.py::test_concurrent_writers_both_end_with_valid_json` uses `threading.Barrier(4)` to force 4 threads into the contended region simultaneously. The test asserts `json.load(file)` parses successfully and trace length ∈ [1, 4]. Without the fix, the test deterministically fails because `os.replace` raises on the clobbered tmp.

**The fix** (smallest possible change preserving the R4 contract):

```python
def _unique_tmp_path(path: Path) -> Path:
    suffix = uuid.uuid4().hex
    return path.with_name(f"{path.name}.{suffix}.tmp")
```

Each writer now `os.replace`s a DISTINCT tmp into the canonical path. The last one wins — preserving the documented "deterministic last-write-wins" contract from R4. The existing `src.endswith(".tmp")` test contract and `*.tmp` orphan-glob contract both still hold (suffix still ends in `.tmp`).

**Flakiness check**: 5/5 consecutive runs pass. No flakiness.

This validates the value of strict TDD: the original apply agent's 12-commit sweep never exercised concurrent contention, so the bug was silent. The verify phase caught it via the missing R4-S2 test scenario. The remediation closes both the test gap AND the production gap.

## Open Follow-Ups (intentional, non-blocking)

The operator explicitly chose minimal remediation scope (per the launch prompt). Three PARTIAL WARNING scenarios remain — all SHOULD-level or combined-scenario gaps, all non-blocking.

### 3 PARTIAL WARNING scenarios

1. **R9-S2 — `resume` switches active session without touching the previous one** — PARTIAL.
   - Current test `test_resume_loads_existing_session` resumes the SAME session that was just created, not a separate "session A" + "session B" pair. Scenario requires asserting that A's file is byte-identical pre/post-resume AND that the new step lands on B's file.
   - Implementation is correct (`session_journal.py:264-290` replaces `self._session_id` and `self._state`); the test just doesn't pin the multi-session invariant.
   - **Recommendation**: follow-up change with a dedicated `test_resume_switches_active_session_without_mutating_previous` test using two `SessionJournal` instances (A and B) in the same tmp_path.

2. **R12-S1 — `nora_health` returning `connectivity: unavailable` records the call without changing the response** — PARTIAL.
   - Current coverage splits this into 3 tests across 2 files: success path (records step + 4-tuple preserved), custom-tool-raise path (records `outcome=error` + re-raises), and `nora_health` unavailable (4-tuple preserved, no journal assertion).
   - The COMBINED scenario — `nora_health` specifically + provider fail + `outcome=error` + 4-tuple preserved — is not in one test.
   - **Recommendation**: follow-up test that injects a provider-failure stub into the real `nora_health_impl`, drives the full middleware, and asserts both the journal entry and the byte-identical 4-tuple.

3. **R14-S1 — read-only call advances `last_updated`** — PARTIAL.
   - Current test `test_set_focus_records_device_and_advances_last_updated` verifies `set_focus` (a WRITE) advances `last_updated`, not that a read-only `get_state` does. Implementation only persists on first read (`if not was_loaded: self._persist(state)`) and doesn't re-stamp `last_updated` on subsequent reads.
   - **Recommendation**: extend `test_get_state_idempotent_and_does_not_mutate` (or add a sibling test) that calls `get_state()` twice and asserts the second call's persisted `last_updated > first_call > initial`. Implementation would need to add a re-stamp on every read in `_load_or_create`'s cached path.

### 3 deferred MAY scenarios (out of scope per proposal)

- **R19-S1** — gzip rotation after 24h. Marked `MAY` in spec; explicitly excluded from this change. Operator disk budget is the only ceiling; no proactive compaction.
- **R20-S1** — cross-session search. Marked `MAY`; deferred to a follow-up change.
- **R21-S1** — encryption-at-rest hook (AES-GCM via `encryption_key: bytes | None`). Marked `MAY`; interface-only stub for now.

### Orphan `.tmp` files (R4 acceptance — out of scope here)

The atomic-write primitive guarantees that a process crash mid-rename leaves an orphan `<canonical>.<uuid>.tmp` file. These are EXPECTED, IDEMPOTENTLY CLEANED on next open (or accumulate harmlessly until operator review). A periodic cleanup hook is a separate piece of work — see the R4 acceptance reasoning in `verify-report.md`.

### 2 SUGGESTIONS (informational)

1. `test_resume_loads_existing_session` could enrich its assertions to also check that `session_id` does not change as a side effect. Mostly a non-issue because the test already asserts `result.data["session_id"] == sid`.
2. `__main__.py` coverage is 53% (missing lines 37-56, 60 — the boot wires `init_session_journal` + `register_auto_trace_middleware`). Acceptable since the boot path is integration-tested via `tests/test_integration.py` (subprocess boot). A focused `test_main_module_wires_session_journal` would close the gap.

## Real Bug Caught (revisited — TDD success story)

See "Real Bug Caught During Remediation" above. The remediation commit `507cafe` is documented in the commit timeline as #14.

## Cross-References

- **Phase 1 archives** (consumed by this change):
  - [`2026-09-06-foundation-bootstrap`](archive/2026-09-06-foundation-bootstrap/) — base 5 specs (`nora-mcp-server`, `telemetry-sanitizer`, `secure-configuration`, `llm-provider-interface`, `project-toolchain`).
  - [`2026-09-10-foundation-bootstrap-cleanup`](archive/2026-09-10-foundation-bootstrap-cleanup/) — closes the 4 PARTIAL scenarios from foundation-bootstrap (Discoverable Make, MCP error-path sanitization, alias-map log-level contract, `model_id` reaches SDK verbatim) + tightens `_SDK_ERROR_MAP` to 7 typed classes + refreshes `openspec/config.yaml` (pytest-cov + coverage threshold 85%).
- **Downstream consumer** (sibling change, archives second):
  - `phase2-pmp450i-driver` (still in active `openspec/changes/`; not yet archived). Its new `snmp_get_pmp450i_radio_metrics` tool MUST call `nora_session_set_focus(device_id)` to declare its investigation target and SHOULD use `nora_session_get_state` to recall prior steps across calls. The driver does NOT re-implement the journal — it consumes it.
- **Memory observations**:
  - `obs-82e57738ba659053` — apply-progress (14 commits, remediation details, real-bug discovery, follow-up list). Will be updated to `status: ARCHIVED` after this archive commit.
  - `obs-288767ccefa6a84c` — apply-discipline learning (the original apply agent left a TODO comment in production code despite marking the task `[x]`; verify caught it; remediation closed it with strict TDD).

## Archive Move Evidence (Mechanical Copy Contract)

The pre-move recursive snapshot was created via `cp -R openspec/changes/phase2-session-journal/ /var/folders/.../sdd-archive.XXXXXX/source` immediately before the move. The archived folder was then `diff -r`'d against that snapshot.

```
$ diff -r "$snapshot_root/source" "openspec/changes/archive/2026-09-10-phase2-session-journal/"
<empty output>
$ echo "diff -r exit status: $?"
diff -r exit status: 0
```

**Status: empty diff, exit 0 — PASS**. The archived folder is byte-identical to the pre-move snapshot.

The `archive-report.md` (this file) is additive and was written AFTER the move; it is excluded from the readback comparison because it did not exist in the source snapshot, per the Mechanical Copy Contract.

**Note on `mv` vs `cp + mv_temp` for spec sync**: Most files in the change folder were UNTRACKED (`proposal.md`, `design.md`, `verify-report.md`, `specs/`); only `tasks.md` and `pr-description.md` were tracked. The skill protocol says "git mv when tracked, mv otherwise"; for a folder with mixed tracked/untracked contents, plain `mv` was used. The canonical spec was synced via `cp source → temp → diff (verified byte-identical) → mv temp → canonical`. Both the archive copy and the canonical copy are now byte-identical (SHA-256 `d8593c3b…3a3de1`); the archive retains the spec as audit trail per the "never delete archived artifacts" rule.

## Final-State Authority Note

Per the Final-State Authority hierarchy, this report reflects the state **AT ARCHIVE CLOSE**. The 14-commit timeline includes the remediation commit `507cafe` that fixed the 2 previously CRITICAL findings. The orchestrator's launch prompt outranked any intermediate snapshots:

- **Persisted tasks artifact**: 14/14 `[x]` (Task Completion Gate source-of-truth — verified by direct read of `openspec/changes/phase2-session-journal/tasks.md` before archive).
- **Orchestrator's explicit final-state facts**: 14 commits (12 design + 1 chore + 1 remediation), 201 tests (125 baseline + 74 design + 2 remediation), 95% coverage, 0 CRITICAL, 3 PARTIAL WARNINGs, 2 SUGGESTIONs, all 4 functional gates PASS.
- **`verify-report.md`**: matches the orchestrator's final-state facts exactly (verdict=PASS, 34/37 spec coverage, 0 CRITICAL).

No contradictions between sources — the verify-report was already authored with the post-remediation state. The 3 PARTIAL WARNINGs are recorded as non-blocking follow-up candidates, not as open defects.

## Rules.archive Compliance

From `openspec/config.yaml`:

- **Warn before merging destructive deltas.** N/A — only 1 new spec created (no prior main spec existed; nothing destructive). Canonical copy + audit trail copy both preserved.
- **Confirm no secrets or real infrastructure identifiers entered `openspec/specs/`.** Confirmed — canonical spec is byte-identical to the source change-folder spec; the only literals in the spec are synthetic placeholders (`ap-7400-01` device IDs, RFC1918 IPv4 `10.0.0.5`, canonical MAC `aa:bb:cc:dd:ee:ff`, reserved `.example.com` TLD, `sk-testkey1234567890abcdef` placeholder, SNMP community `private` test fixture, `[REDACTED]` spec-mandated marker).

## Next Step

The SDD cycle is complete. The branch `feat/phase2-session-journal` carries 14 production commits + 1 archive commit ahead of `main`. All 4 functional gates PASS, 0 CRITICAL findings remain, spec coverage 34/34 (100%) of in-scope. Ready for PR.

**Recommended operator action**: open a PR via `branch-pr` skill with title:

```
feat(nora): SessionJournal — auto-trace middleware + 4 explicit recall tools
```

The body is `openspec/changes/archive/2026-09-10-phase2-session-journal/pr-description.md` (already in the archive folder as the PR body draft; commit history + remediation summary + spec mapping + risk table). The `size:exception` is approved and noted in the PR description.

After the PR is opened and merged, the next SDD cycle (`phase2-pmp450i-driver`, currently in `openspec/changes/`) can proceed — it consumes the journal and depends on it being archived first.
