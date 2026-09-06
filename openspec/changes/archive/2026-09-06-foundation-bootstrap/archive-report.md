# Archive Report — foundation-bootstrap

**Archived**: 2026-09-06
**Verdict at archive**: PASS WITH WARNINGS
**Tasks at archive**: 25/25 `[x]` (18 original + 7 Phase 7 remediation)
**Review Gate**: `reviewGate` structurally ABSENT — no review was started for this candidate; archive proceeds under ordinary repository policy.

## Final State

- **Production code**: `src/nora/` = 800 lines exactly (at proposal budget ceiling, zero headroom)
- **Test count**: 103 (98 original + 5 new sanitization tests in `tests/test_llm.py:350-432`)
- **Static gates**: `ruff check`, `ruff format --check`, `mypy --strict src/nora` → exit 0 on every run
- **Findings**: 0 CRITICAL, 0 blockers, 3 WARNINGs (non-blocking, carried forward)

## Specs Synced

All 5 delta specs were FULL specs (no prior `openspec/specs/` main spec existed). Each was mechanically copied from `openspec/changes/foundation-bootstrap/specs/<cap>/spec.md` to `openspec/specs/<cap>/spec.md` via `cp` + per-file `diff` readback + atomic `mv` rename.

| Domain | Action | Path |
|--------|--------|------|
| project-toolchain | Created (no prior main spec) | `openspec/specs/project-toolchain/spec.md` |
| secure-configuration | Created | `openspec/specs/secure-configuration/spec.md` |
| telemetry-sanitizer | Created | `openspec/specs/telemetry-sanitizer/spec.md` |
| llm-provider-interface | Created | `openspec/specs/llm-provider-interface/spec.md` |
| nora-mcp-server | Created | `openspec/specs/nora-mcp-server/spec.md` |

Per-file post-sync `diff -q` verification: **all 5 specs synced identically**.

## Archived Folder Contents

```
openspec/changes/archive/2026-09-06-foundation-bootstrap/
├── proposal.md
├── design.md
├── tasks.md               (25/25 [x], 0 unchecked)
├── verify-report.md
├── explore.md
├── specs/
│   ├── project-toolchain/spec.md
│   ├── secure-configuration/spec.md
│   ├── telemetry-sanitizer/spec.md
│   ├── llm-provider-interface/spec.md
│   └── nora-mcp-server/spec.md
└── archive-report.md      (this file — additive, written post-move)
```

## WARNINGS (non-blocking, carried forward per `verify-report.md`)

1. **W1** — 4 scenarios PARTIAL coverage (Discoverable Make, MCP output sanitized, Replacement Counter log-level contract, free-text sanitized error path). Source review confirms correct implementation; missing coverage would only surface under specific regressions.
2. **W2** — Source LOC at 800 ceiling, zero headroom for future patches within the same budget.
3. **W3** — `_SDK_ERROR_MAP = (Exception,)` in `src/nora/llm.py:85` is broader than the design's stated intent. Behavior correct.

## Deviations (3 ACCEPTABLE, carried forward)

1. `pytest 9` stdout/stderr behavior; integration test accepts either stream.
2. FastMCP stdio line-read via `threading.Thread` + `queue.Queue` (no fixed delay).
3. `Settings.loaded_from` set via custom `__init__` (defensive; also has `model_validator` fallback).

## Prior-Phase Findings Resolution

| Finding | Resolution | Evidence |
|---------|-----------|----------|
| CRITICAL #2 (sanitizer not wired) | RESOLVED | `_SAN: Sanitizer = Sanitizer()` at `src/nora/llm.py:84`; both `LMStudioProvider.complete()` (line 126) and `GeminiProvider.complete()` (line 183) pass `_SAN.sanitize(prompt).text` to SDK; 5 new tests at `tests/test_llm.py:350-432` |
| CRITICAL #1a (flaky integration test) | RESOLVED | `threading.Thread` + `queue.Queue` line-read in `tests/test_integration.py:32-54,118-173`; 5/5 runs passed (2.74 / 2.59 / 2.39 / 2.41 / 2.28 s) |
| CRITICAL #1b (flaky coverage test) | RESOLVED | per-PID `COVERAGE_FILE` isolation in `tests/test_toolchain.py:184-203`; 5/5 runs passed |
| CRITICAL #3 (broken `apply.test_command`) | RESOLVED | `openspec/config.yaml:120,122` now `uv run python -m pytest` (both `apply.test_command` and `verify.test_command`) |

## Delivery Decision

- **delivery_strategy**: `exception-ok` (size:exception)
- **chain_strategy**: `size-exception`
- Single PR; NOT chained. User explicitly accepted the `size:exception` during `sdd-tasks`.

## Archive Move Evidence

The pre-move recursive snapshot (`/var/folders/z2/jfkx5rs11w9c7546250wxl5c0000gn/T//sdd-archive.I21Ll1/source`) was diffed against the archived folder (`openspec/changes/archive/2026-09-06-foundation-bootstrap/`). The verbatim `diff -r` output appears below — empty diff is the only passing evidence.

```
$ diff -r "$snapshot_root/source" "openspec/changes/archive/2026-09-06-foundation-bootstrap/"
<empty output>
$ echo "diff -r exit status: $?"
diff -r exit status: 0
```

Status: **empty diff, exit 0 — PASS**. The archived folder is byte-identical to the pre-move snapshot.

The `archive-report.md` (this file) is additive and was written AFTER the move; it is excluded from the readback comparison because it did not exist in the source snapshot, per the Mechanical Copy Contract.

## Final-State Authority Note

Per the Final-State Authority hierarchy, this report reflects the state **AT ARCHIVE CLOSE**. The 3 WARNINGs, 3 deviations, and all 4 prior CRITICALs were resolved before archive. Intermediate snapshots (`apply-progress`, `verify-report`) describe earlier states; the orchestrator's launch prompt was treated as the most-recent final-state account and outranked those snapshots where they disagreed (none did in practice — verify-report already records the post-remediation state).

- 25/25 tasks `[x]` in the persisted `tasks.md` (source-of-truth task gate).
- 103 tests / 800 LOC / 0 CRITICAL / 3 WARNINGs from the orchestrator prompt (final-state facts).
- 0 unchecked tasks in the archived `tasks.md` confirmed by `grep -c '^- \[ \]'` = 0.

## Next Step

The SDD cycle is complete. Ready for the next change.
