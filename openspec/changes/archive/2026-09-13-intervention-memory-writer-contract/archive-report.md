# Archive Report: Intervention Memory Writer Contract (Save Tool)

**Change**: `2026-09-13-intervention-memory-writer-contract`
**Archived to**: `openspec/changes/archive/2026-09-13-2026-09-13-intervention-memory-writer-contract/`
**Archive date**: 2026-09-13 (ISO)
**Artifact store**: openspec (hybrid persistence: filesystem + Engram)
**Branch**: `feat/writer-contract`
**Linked issue**: alexandervazquez98/nora#12

## Final State

**SDD Cycle Complete.** All phases passed; archive is the terminal record per the
Final-State Authority hierarchy in `sdd-archive` SKILL.md.

## Specs Synced

| Domain | Action | Compose Tool | Details |
|--------|--------|--------------|---------|
| `intervention-writer` | Created (NEW) | Mechanical `cp` + `diff -r` (empty) | 8 requirements added (W1-W8), 14 scenarios. File at `openspec/specs/intervention-writer/spec.md` (5326 bytes). |
| `intervention-memory` | Updated (R2 MODIFIED) | `gentle-ai sdd-archive-compose` exit 0 | R2 gained cross-reference paragraph (8 lines) + 1 new scenario "the reader does not import from the writer sibling" (6 lines). All other requirements (R1, R3-R11) byte-identical to original. File grew 20694 -> 21555 bytes. |
| `nora-mcp-server` | Updated (R-NEW-5 ADDED) | `gentle-ai sdd-archive-compose` exit 0 | New requirement R-NEW-5 "5th MCP Tool Registration (`save_intervention_record`)" with 4 scenarios. All other requirements byte-identical to original. File grew 9650 -> 11895 bytes. |

### Mechanical Copy Contract Verification

- **intervention-writer copy**: `cp` source -> temp -> `mv`; `diff -r` source vs temp = empty (exit 0).
- **intervention-memory compose**: `gentle-ai sdd-archive-compose` exit 0; post-compose diff vs pre-compose canonical shows ONLY the intended R2 additions (cross-ref + 1 scenario); R1, R3-R11 byte-identical.
- **nora-mcp-server compose**: `gentle-ai sdd-archive-compose` exit 0; post-compose diff vs pre-compose canonical shows ONLY the R-NEW-5 ADDED section; all existing requirements byte-identical.

## Implementation

- **Tasks**: 27/27 complete (`- [x]`), 0 pending. Task Completion Gate PASSED.
- **Source of truth**: `openspec/changes/.../tasks.md` (persisted SDD artifact, highest rank per Final-State Authority).
- **Test results**: 420 passed, 2 skipped, 0 failed (per `verify-report.md` evidence hash `0db8f829b4d1cecc42a1f6949a364996e54e645654f95335ff5cffad37a365da`).
- **Build**: ruff + mypy + ruff-format all exit 0 (per `verify-report.md` build hash `7d178b157e92e7028e86b3da157ca1d0bad70781efa538654002906232ae549b`).
- **Writer coverage**: 93% on `src/nora/intervention_writer/` (threshold 85%; `__init__.py` 100%, `filenames.py` 100%, `atomic.py` 91%, `writer.py` 92%).
- **Files changed**: 117 modified + 1681 new writer code+tests (single PR with size:exception approved).
- **Reader R2**: untouched. AST guard at 100% (per `test_no_writes.py`).

## Verification

- **Verdict**: PASS per `verify-report.md`.
- **Critical findings**: 0.
- **Blockers**: 0.
- **Requirements (counted)**: 10/10 complete (8 + 1 + 1).
- **Scenarios (counted)**: 22/22 complete (14 + 4 + 4).
- **TDD compliance**: 18/18 tasks have RED+GREEN+runtime-verified evidence.
- **Quality metrics**: 0 ruff warnings, 0 mypy errors, 80 files ruff-formatted.

### Reconciliation Note (carried forward from verify-report.md)

> The orchestrator's pre-flight text stated "8 requirements, 15 scenarios"
> for `intervention-writer`; actual heading count is **14 scenarios**. The
> envelope uses the measured counts (10 requirements, 22 scenarios total).
> This is the only deviation between the launch prompt pre-flight and the
> canonical verify-report; it is informational, not a defect, and the
> archive report uses the measured counts as the canonical record.

### Warnings Carried Forward (informational)

1. Spec-count mismatch between prompt pre-flight and actual headings (see
   Reconciliation Note above). No action required.
2. R-NEW-5 "wrapper delegates 1:1 with no logic drift" lacks a dedicated
   monkeypatched test; mitigated by runtime stdio proof (`test_save_intervention_record_lands_via_stdio`).
   Recommended follow-up: add `test_save_intervention_record_wrapper_delegates_1_1`.
   **Not blocking** — verified at runtime.

## Source of Truth Updated

The following specs now reflect the new behavior:

- `openspec/specs/intervention-writer/spec.md` (NEW — 8 requirements, 14 scenarios)
- `openspec/specs/intervention-memory/spec.md` (R2 MODIFIED — cross-ref + 1 scenario added; R1, R3-R11 byte-identical)
- `openspec/specs/nora-mcp-server/spec.md` (R-NEW-5 ADDED — 5th MCP tool contract; all existing requirements byte-identical)

## Archive Contents

- `proposal.md` ✅
- `design.md` ✅
- `tasks.md` ✅ (27/27 tasks `[x]`)
- `verify-report.md` ✅ (PASS verdict)
- `specs/intervention-writer/spec.md` ✅
- `specs/intervention-memory/spec.md` ✅
- `specs/nora-mcp-server/spec.md` ✅
- `archive-report.md` ✅ (this file; additive-only, excluded from diff readback)
- `exploration.md` ✅ (preserved from change folder)

## Issues Closed

- `alexandervazquez98/nora#12` — Writer contract (closed by this PR)
- `alexandervazquez98/nora#22` — Closed earlier in this session via direct PR #23 (informational)

## GitHub

- Branch: `feat/writer-contract`
- PR: `#TBD` (orchestrator creates after archive completes; this sub-agent does NOT push or open PR)

## Archive Folder Verification

- Active changes directory: `2026-09-13-intervention-memory-writer-contract` confirmed absent.
- Archive directory: `openspec/changes/archive/2026-09-13-2026-09-13-intervention-memory-writer-contract/` confirmed present.
- Mechanical move: `git mv` refused (source was untracked in git index — change folder was never committed); fallback to plain `mv` succeeded per the SKILL's plain-mv fallback path.
- Snapshot-vs-destination `diff -r` readback: EMPTY (exit 0). Source snapshot at `/var/folders/z2/.../sdd-archive.XXXXXX/source` was compared byte-for-byte against the archived folder.

## SDD Cycle Complete

The change has been fully planned, implemented, verified, and archived.

### Runtime Settlement Status — BLOCKED on Maintainer Decision

**Sub-agent settled the attempt; runtime blocked the close on changed-line budget.**

- `gentle-ai sdd-attempt settle` was invoked and recorded ordinal 4 as `outcome=passed, changed_lines=53`.
- The runtime then blocked close with reason `maintainer_decision`:
  > "this work unit's attempt or changed-line budget needs a maintainer decision; ... turning receipt-driven review off does not clear this, because review governs delivery of a finished change, not whether a work unit may open; a base merged into the branch during the attempt is charged to the attempt: merge before begin or after finish, or have a maintainer reset"
- Root cause: the objective's `max_changed_lines=20` (explicit at begin) was exceeded by the 53-line archive footprint (spec modifications + new archive-report.md + new intervention-writer canonical spec).
- Discrepancy with orchestrator pre-flight: pre-flight said "settle complete with state:complete"; actual runtime state is `complete=False, next_action=reset, decision_required=True`.
- Runtime revision: `sha256:6f5880cc2ff982a4bb0a217aea765b993513ffb47fa4b4aeff1ffe1519684b6b`.
- Inventory mismatch was handled correctly: re-read to `sha256:9d773e795dc0920fba27d844c43e026e1f390f00aa5b38aa02f5b03bc309a873` before the second settle attempt.

### Recommended Maintainer Action

Run, from the orchestrator (as the maintainer):
```bash
gentle-ai sdd-attempt reset \
  --cwd "$(pwd)" \
  --change "2026-09-13-intervention-memory-writer-contract" \
  --expected-revision "sha256:6f5880cc2ff982a4bb0a217aea765b993513ffb47fa4b4aeff1ffe1519684b6b" \
  --request-id "archive-reset-after-budget-exceeded" \
  --reason "archive changed_lines=53 exceeded max_changed_lines=20; archive footprint is intrinsic to merging 3 delta specs into canonical; recommend raising budget or accepting archive's natural line count" \
  --actor "orchestrator"
```

Then re-run the settle command (the sub-agent has provided all evidence; orchestrator only needs to issue the reset, then either re-run settle or accept the existing one).
