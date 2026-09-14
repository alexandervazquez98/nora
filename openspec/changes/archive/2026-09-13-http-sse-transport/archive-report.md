# Archive Report: HTTP/SSE Transport for nora-mcp

**Change**: `2026-09-13-http-sse-transport`
**Archived to**: `openspec/changes/archive/2026-09-13-http-sse-transport/`
**Archive date**: 2026-09-13 (ISO)
**Artifact store**: openspec
**Branch**: `feat/http-sse-transport` (12 commits ahead of `origin/main` after this archive commit)
**Linked issue**: alexandervazquez98/nora#34 — **NOT closed by archive; closes via `Closes #34` keyword on the PR body when the orchestrator merges**

## Final State

**SDD Cycle Complete.** All phases passed; archive is the terminal record per the
Final-State Authority hierarchy in `sdd-archive` SKILL.md.

## Specs Synced

| Domain | Action | Compose Tool | Details |
|--------|--------|--------------|---------|
| `nora-mcp-server` | Updated (1 RENAMED + 1 MODIFIED + 1 ADDED) | `gentle-ai sdd-archive-compose` exit 0 (with working-delta `RENAMED` wrapper) | `FastMCP Boot Over Stdio` → `FastMCP Boot With Configurable Transport` (6 scenarios); new `Systemd Loads Transport Env File` (2 scenarios). All 11 unrelated requirements preserved byte-for-byte. File grew 11895 → 15006 bytes (+3111). |

### Compose Invocation (verbatim)

The `sdd-archive-compose` tool rejects MODIFIED deltas whose heading text does not exactly
match a canonical heading. The change's MODIFIED block uses the post-rename name
`FastMCP Boot With Configurable Transport`, but the canonical heading at apply time was
still `FastMCP Boot Over Stdio`. To keep the merge atomic through the native compose
tool (rather than a model-driven Read/Edit merge), a transient working delta was built
in a tmpdir with a `## RENAMED Requirements` section placed BEFORE the existing MODIFIED
section. Compose applies RENAMED → MODIFIED → REMOVED → ADDED, so the rename ran first,
making the MODIFIED heading name resolvable. The working delta is NOT a persisted SDD
artifact and is deleted after composition.

```bash
# 1. Build working delta with RENAMED + MODIFIED + ADDED
WORKING_DELTA="/tmp/.../working-delta.md"   # transient, deleted after step 3

# 2. Atomic compose — exit 0 is the only passing evidence
gentle-ai sdd-archive-compose \
  --canonical "openspec/specs/nora-mcp-server/spec.md" \
  --delta "$WORKING_DELTA" \
  --output "/tmp/.../spec.composed.md"
# compose_exit=0; output 15006 bytes

# 3. Atomic swap into canonical (compose-tmp + mv, byte-identical)
cp "$WORKING_DELTA_OUTPUT" "openspec/specs/nora-mcp-server/spec.md.compose-tmp"
diff -r "$WORKING_DELTA_OUTPUT" "openspec/specs/nora-mcp-server/spec.md.compose-tmp"   # empty (exit 0)
mv "openspec/specs/nora-mcp-server/spec.md.compose-tmp" "openspec/specs/nora-mcp-server/spec.md"
diff -r "$WORKING_DELTA_OUTPUT" "openspec/specs/nora-mcp-server/spec.md"              # empty (exit 0)
# Pre: 11895 bytes; Post: 15006 bytes; delta +3111
```

### Mechanical Copy Contract Verification

- `gentle-ai sdd-archive-compose` exit 0 (the only passing evidence).
- Compose-tmp vs composed-output `diff -r`: **empty (exit 0)**.
- Post-`mv` canonical vs composed-output `diff -r`: **empty (exit 0)**.
- Heading-by-heading audit: 12 prior requirements preserved byte-for-byte; 1 renamed in-place; 1 added at end; 0 unrelated requirements dropped; 0 unintended requirements added. Total requirements: 12 → 13. Total scenarios: 16 → 24.

## Task Completion Gate

The persisted `tasks.md` arrived with stale `- [ ]` checkboxes on completed sub-tasks (T1–T10). The verify-report (PASS verdict) proves every T1–T10 sub-task is complete at runtime. T11 (PR creation + Closes #34) is explicitly owned by the orchestrator per the launch prompt: "Implementation branch: `feat/http-sse-transport` (11 commits ahead of `origin/main`, NOT pushed, NOT merged yet — orchestrator handles PR creation)". Per `sdd-archive` SKILL.md, archive may perform exceptional mechanical reconciliation only with proof from apply-progress/verify-report; that proof is present.

**Reconciliation performed (recorded here as required)**:

- T1.1, T1.2, T1.3 → `[x]` (RED scaffold tests committed; confirmed at commit `9c076f4`)
- T2.1, T2.2, T2.3, T2.4 → `[x]` (GREEN cli.py dispatch; coverage 96% per verify-report)
- T3.1, T3.2 → `[x]` (`.env.mcp.example` shipped; verified by grep)
- T4.1, T4.2, T4.3, T4.4 → `[x]` (`phase_env_file_mcp()` ships; `bash -n` exit 0)
- T5.1, T5.2, T5.3 → `[x]` (`--force-transport-env` flag forwarded; `bash -n` exit 0)
- T6.1, T6.2 → `[x]` (second `EnvironmentFile=` line in unit; `grep -c` = 2)
- T7.1, T7.2, T7.3, T7.4 → `[x]` (`--check-http` opt-in probe; `bash -n` exit 0)
- T8.1, T8.2 → `[x]` (`httpx>=0.27` pinned in dev deps; lock regenerated)
- T9.1, T9.2, T9.3, T9.4 → `[x]` (485 tests pass, ruff+mypy+bash all exit 0, manual smoke green)
- T10.1, T10.2, T10.3, T10.4 → `[x]` (ADR shipped, INSTALL+OPERATIONS updated)
- **T11.1, T11.2 → `[ ]`** (PR creation deferred to orchestrator — NOT archived work)

Tally post-reconciliation: **32 `[x]` / 2 `[ ]`** (T11 only).

## Implementation

- **Source of truth**: `openspec/changes/.../tasks.md` (persisted SDD artifact, highest rank per Final-State Authority).
- **Test results**: 485 passed, 3 skipped, 0 failed (per `verify-report.md`; 1 pre-existing flaky test in `test_toolchain.py` deselected — fails identically on `origin/main` HEAD `24c8d20`).
- **Build**: ruff + ruff-format + mypy --strict + `bash -n` (3 shell scripts) all exit 0.
- **Coverage**: `src/nora/cli.py` 96% (above 95% design target); `src/nora` total 87% (above 85% `openspec/config.yaml` threshold).
- **Files changed in implementation branch**: 14 modified + 0 removed.

## Verification

- **Verdict**: PASS per `verify-report.md`.
- **Critical findings**: 0.
- **Blockers**: 0.
- **Requirements (counted post-compose)**: 13 (1 RENAMED + 1 MODIFIED + 1 ADDED + 10 preserved).
- **Scenarios (counted post-compose)**: 24 (16 preserved + 6 MODIFIED + 2 ADDED).
- **TDD compliance**: 5/6 checks passed (Safety-Net partial — old "no `transport=` in src" invariant intentionally superseded by the new requirement; mitigated by the 10 new tests covering the new contract).

### Warnings Carried Forward (informational)

1. **Pre-existing flaky test deselected**: `tests/test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed` times out at 60s on this branch. Verified to also fail on `origin/main` HEAD `24c8d20` with the same `subprocess.TimeoutExpired` — NOT a regression introduced by this change. Out of scope for archive.

2. **Apply-progress test count off by 1**: Apply-progress memory claimed "486 tests passing"; actual measured count is **485 + 3 skipped + 1 deselected**. Likely from commit `4fe7da8 test(cli): share _install_cli_stubs helper + format + smoke assertion` which consolidated helpers without changing test functions.

3. **Design doc deviation not yet updated**: `design.md` § Module (lines 55-65) shows a uniform `mcp.run(transport=..., host=..., port=..., path=..., stateless_http=..., show_banner=False)` form. The implementation branches at `src/nora/cli.py:265-278` to omit network kwargs for stdio (commit `d26655b fix(cli): forward network kwargs only when transport is non-stdio` documents the rationale — FastMCP's `run_stdio_async()` rejects them with `TypeError`). Tests don't pin the shape so all pass. Recommended follow-up: update `design.md` § Module to document the branch — non-blocking for archive.

## Source of Truth Updated

The following canonical spec now reflects the new behavior:

- `openspec/specs/nora-mcp-server/spec.md` (15006 bytes; 13 requirements, 24 scenarios)

## Archive Contents

- `proposal.md` ✅
- `design.md` ✅
- `tasks.md` ✅ (32/34 tasks `[x]`; T11.1, T11.2 `[ ]` — orchestrator-owned PR creation)
- `verify-report.md` ✅ (PASS verdict; 0 critical findings)
- `exploration.md` ✅ (preserved from change folder)
- `specs/nora-mcp-server/spec.md` ✅ (original delta kept for history)
- `archive-report.md` ✅ (this file; additive-only, excluded from diff readback)

## Issues Closed

- `alexandervazquez98/nora#34` — HTTP/SSE transport — **NOT closed by this archive commit**.
  Closes via the PR description's `Closes #34` keyword when the orchestrator merges the
  PR from `feat/http-sse-transport` to `main`. Archive leaves the issue state unchanged.

## GitHub

- Branch: `feat/http-sse-transport`
- PR: `#TBD` (orchestrator creates after archive completes; this sub-agent does NOT push or open PR)
- Commits added by this archive: **1** (`chore(archive): close 2026-09-13-http-sse-transport`)
- Branch commits ahead of `origin/main`: **12** (11 implementation + 1 archive)

## Archive Folder Verification

- Active changes directory `openspec/changes/2026-09-13-http-sse-transport/` confirmed **absent**.
- Archive directory `openspec/changes/archive/2026-09-13-http-sse-transport/` confirmed **present** (6 files: design.md, exploration.md, proposal.md, specs/, tasks.md, verify-report.md + archive-report.md).
- Mechanical move: `git mv` refused (source was untracked in git index — change folder was never committed in this branch). Fallback to plain `mv` succeeded per the SKILL's plain-mv fallback path. Pre-move snapshot vs post-move destination: `diff -r` readback **EMPTY (exit 0)**.

## SDD Cycle Complete

The change has been fully planned, implemented, verified, and archived.
Ready for the orchestrator's next phase: push `feat/http-sse-transport` and open the PR (with `Closes #34` in the body).
