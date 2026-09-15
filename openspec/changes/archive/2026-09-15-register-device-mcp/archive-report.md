# Archive Report: register_device MCP tool + ad-hoc device registration

**Change**: `2026-09-15-register-device-mcp`
**Archived to**: `openspec/changes/archive/2026-09-15-register-device-mcp/`
**Archive date**: 2026-09-15 (ISO)
**Artifact store**: openspec
**Branch**: `feat/register-device-mcp` (10 implementation commits ahead of `origin/main` after this archive commit; matches preflight `commits_expected=10`)
**Linked issue**: alexandervazquez98/nora#42 — IPv4-literal resolution gap — **NOT closed by archive; closes via `Closes #42` keyword on the PR body when the orchestrator merges**
**Apply size**: 1893 LoC production+tests (size:exception acknowledged by maintainer; actual diff: 1847 insertions / 46 deletions across 32 files, includes 6 generated catalog goldens)

## Final State

**SDD Cycle Complete.** All phases passed; archive is the terminal record per the
Final-State Authority hierarchy in `sdd-archive` SKILL.md.

## Specs Synced

| Domain | Action | Compose Tool | Details |
|--------|--------|--------------|---------|
| `nora-mcp-server` | Updated (1 RENAMED + 2 MODIFIED + 0 ADDED; 3 requirements touched) | `gentle-ai sdd-archive-compose` exit 0 | R-NEW-6 renamed `Tool-Registration Guard (Uncatalogued Tools Rejected)` → `Tool-Registration Guard (Catalog Re-Signed)`; R-NEW-1 + R-NEW-2 body updated (eleven → twelve); 12 unrelated requirements preserved byte-for-byte. File grew 16091 → 18822 bytes (+2731). 32 prior scenarios preserved + 6 net new scenarios (R-NEW-1 +0, R-NEW-2 +2, R-NEW-6 +3) = 38 total. |
| `ad-hoc-device-registration` | Created (NEW capability) | Mechanical copy (canonical did not exist) | Full spec written from delta; 4 ADDED requirements (MutableInventory wrapper, `register_device` MCP tool, Orchestrator Prompt Fallback, `data/devices.yaml` Gitignore); 18 scenarios. File: 8251 bytes. |

### Compose Invocation (verbatim)

The single change-root `spec.md` uses a non-standard format (`## MODIFIED Requirements (under \`nora-mcp-server\`)` with parenthetical section header) that the compose parser does not recognise — the parser regex is `(?m)^## (ADDED|MODIFIED|REMOVED|RENAMED) Requirements[ \t]*$`, which fails on the parenthetical suffix. The change's `spec.md` also lists MODIFIED + ADDED sections in one file rather than the per-domain `openspec/changes/{change}/specs/{domain}/spec.md` layout the tool expects. Per past NORA archive convention (`2026-09-13-http-sse-transport`, `2026-09-12-nora-mcp-thin-split`, `2026-09-12-secure-config-reissue`, etc.), the deltas were restructured into per-domain files at:

- `openspec/changes/2026-09-15-register-device-mcp/specs/nora-mcp-server/spec.md`
- `openspec/changes/2026-09-15-register-device-mcp/specs/ad-hoc-device-registration/spec.md`

The original change-root `spec.md` is preserved in the archive as-is.

R-NEW-6 required a heading change (canonical: `(Uncatalogued Tools Rejected)` → delta wanted `(Catalog Re-Signed)`). The compose tool refuses silently-merged MODIFIED blocks whose heading text does not exactly match canonical, so a `## RENAMED Requirements` block was added ahead of the `## MODIFIED Requirements` block in the per-domain delta for `nora-mcp-server`. Compose applies RENAMED → MODIFIED → REMOVED → ADDED, so the rename ran first, making the MODIFIED heading name resolvable. The RENAMED block carries both `(Reason: …)` and `(Migration: …)` notes per the compose parser's expectations.

R-NEW-1 and R-NEW-2 carried `(Updated Count)` parentheticals that did not match the canonical headings — those suffixes were stripped from the per-domain delta headings so MODIFIED could find the requirements.

```bash
# 1. Per-domain delta for nora-mcp-server (RENAMED + MODIFIED)
canonical="openspec/specs/nora-mcp-server/spec.md"
delta="openspec/changes/2026-09-15-register-device-mcp/specs/nora-mcp-server/spec.md"
compose_tmp="${canonical}.compose-tmp"

# 2. Atomic compose — exit 0 is the only passing evidence
gentle-ai sdd-archive-compose \
  --canonical "$canonical" \
  --delta "$delta" \
  --output "$compose_tmp"
# compose_exit=0; output 18822 bytes

# 3. Snapshot composed output (for readback)
mkdir -p /tmp/compose-snap-nms
cp "$compose_tmp" /tmp/compose-snap-nms/composed.md
# snapshot 18822 bytes; compose-tmp 18822 bytes (byte-identical)

# 4. Atomic swap into canonical (compose-tmp + mv)
mv "$compose_tmp" "$canonical"
# Pre: 16091 bytes; Post: 18822 bytes; delta +2731

# 5. Mandatory readback
diff -r /tmp/compose-snap-nms/composed.md "$canonical"   # empty (exit 0)
```

```bash
# For ad-hoc-device-registration — canonical did not exist (mechanical copy path)
target_dir="openspec/specs/ad-hoc-device-registration"
target_path="$target_dir/spec.md"
delta="openspec/changes/2026-09-15-register-device-mcp/specs/ad-hoc-device-registration/spec.md"

mkdir -p "$target_dir"
# mktemp + cp + diff -r + mv pattern from sdd-archive SKILL.md
# Final canonical: 8251 bytes (byte-identical to delta)
diff -r "$delta" "$target_path"   # empty (exit 0)
```

### Mechanical Copy Contract Verification

- `gentle-ai sdd-archive-compose` exit 0 (the only passing evidence for the nora-mcp-server compose).
- Composed snapshot vs post-`mv` canonical `diff -r`: **empty (exit 0)** for nora-mcp-server.
- Delta vs post-`cp + mv` canonical `diff -r`: **empty (exit 0)** for ad-hoc-device-registration (mechanical copy path).
- Pre-move snapshot vs post-move archive `diff -r`: **empty (exit 0)** for the change folder move.
- Heading-by-heading audit: 12 unrelated nora-mcp-server requirements preserved byte-for-byte; 3 modified in-place (R-NEW-1, R-NEW-2 body; R-NEW-6 renamed + body); 4 ADDED in the new capability. 0 unrelated requirements dropped; 0 unintended requirements added.
- Total nora-mcp-server: 15 requirements (12 prior + 3 modified/renamed). 38 scenarios (32 prior + 6 net new).
- Total ad-hoc-device-registration: 4 requirements, 18 scenarios (NEW).

## Task Completion Gate

The persisted `tasks.md` uses numbered task headers (`## 1. …`, `## 2. …`, …) rather than `- [ ]` checkboxes (different convention from earlier NORA changes). 10 work-unit tasks (T1–T10) are complete at HEAD `f6c7751` per the apply-progress memory #13225 and the verify-report. T11 (operator-side manual radio sanity check) is explicitly owned by the operator post-merge per the launch prompt and `tasks.md` §11.

Per `sdd-archive` SKILL.md, the Task Completion Gate requires inspecting the persisted tasks artifact for unchecked `- [ ]` items — **none present** in `tasks.md` (this change uses numbered headers). No reconciliation needed; completion is evidenced by:

1. `apply-progress` memory #13225 (project: nora, scope: project, topic: `sdd/2026-09-15-register-device-mcp/apply-progress`): all 10 work-unit commits recorded with commit SHAs (463b052, 1ac0101, 7d4b3e1, 3e5d2e0, 89670df, efbd3f6, e5bd190, 4af770d, eb6ef54, f6c7751); Task 11 deferred.
2. `verify-report.md` verdict PASS with 28/28 spec scenarios covered at HEAD `f6c7751`.

**Tally**: 10/10 work-unit tasks complete; T11 deferred (operator-side, post-merge).

## Implementation

- **Source of truth**: engram #13225 (`sdd/2026-09-15-register-device-mcp/apply-progress`, persisted SDD artifact, highest rank per Final-State Authority).
- **Test results**: 527 passed, 3 skipped, 0 NEW failures (per `verify-report.md`; 1 pre-existing flake in `tests/test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed` deselected via `--deselect` — confirmed pre-existing on `main` HEAD `4e9afce` via a fresh worktree; the standalone re-run on this branch passed in 58s with `--timeout=120`).
- **Build**: ruff + ruff-format + mypy --strict all exit 0 (104 files formatted; 41 source files typed; no issues found).
- **Coverage**: 88% whole-tree (threshold 85%). New cluster: `src/nora/drivers/mutable_inventory.py` 95%; `src/nora/drivers/snmp_pmp450i/register_device.py` 87%; `src/nora/drivers/exceptions.py` 100%; `src/nora/drivers/oid_catalog.py` 85%.
- **Files changed in implementation branch**: 28 modified + 4 new (mutable_inventory.py, register_device.py, test_mutable_inventory.py, test_register_device.py + 3 new test files for gitignore / prompts / register_device boot integration).
- **Generated goldens**: 6 re-signed PMP 450i catalog files (3 operator root + 3 built-in root) carrying the `register_device` envelope and HMAC for `15.2.1`, `15.3.0`, `25.1.0`.

## Verification

- **Verdict**: PASS per `verify-report.md`.
- **Critical findings**: 0.
- **Blockers**: 0.
- **Requirements (counted post-compose)**: 15 (nora-mcp-server) + 4 (ad-hoc-device-registration) = 19 (12 prior nora-mcp-server preserved + 3 modified + 4 ADDED).
- **Scenarios (counted post-compose)**: 38 (nora-mcp-server, 32 prior preserved + 6 net new) + 18 (ad-hoc-device-registration) = 56.
- **TDD compliance**: Strict TDD mode active per preflight; every one of the 10 work-unit commits includes a RED → GREEN sequence (asserted in commit body and per-task tests).

### Final-State Facts and Outstanding Items (residual follow-ups)

The verify-report flagged 4 WARNINGs as residual design follow-ups. None were fixed in this change. Forward to archive that these remain **OPEN** (tracked for future changes):

1. **Pre-existing test flake deselected**: `tests/test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed` hits the 60s subprocess timeout boundary on slow runners. Verified pre-existing on `main` HEAD `4e9afce` via a fresh worktree (same `subprocess.TimeoutExpired`). Standalone re-run with `--timeout=120` passes in 58s on this branch. **Root cause TBD** — recommend follow-up issue to relax the subprocess timeout in the test fixture from 60s to 120s.

2. **Stale test function names + docstrings**: `tests/test_integration.py::test_subprocess_responds_to_tools_list_with_nine_tools`, `tests/test_integration_boot.py::test_subprocess_nora_mcp_exposes_nine_tools`, and `tests/test_main_alias.py` (lines 9, 60, 177) still reference "nine" or "eleven" tool surfaces in their names and comments. Assertions are correct (12 tools) but the docstrings and function names are stale. **Cosmetic rename follow-up**.

3. **Coverage gap in `server.py`**: `src/nora/server.py` is at 73% line coverage (whole-tree average 88% still passes the 85% `openspec/config.yaml` threshold). Uncovered lines are mostly the MCP tool wrappers' `model_dump(mode="json")` returns and the async `_enumerate_tool_names` path. The boot-time guard (`verify_tools_are_catalogued`) is exercised by `tests/test_integration.py::test_rogue_tool_rejected_at_boot` (subprocess) but the in-process path is not. **Recommend follow-up**: add in-process positive-path test for `verify_tools_are_catalogued`.

4. **§4 prompt drift**: `src/nora/prompts/netops_orchestrator.md` §4 Step 4 lists only `register_device` (the new fallback clause added by this change). The other 6 tools absent from §4 are: `snmp_get_ap_summary`, `snmp_get_frame_utilization`, `snmp_get_sm_table`, `snmp_get_sm_detailed_diagnostics`, `snmp_run_spectrum_analysis`, `snmp_migrate_radio_frequency`. This drift is pre-existing (flagged in `explore.md §7.4` and `tasks.md §11 risks`) and out of scope for this change per design §1 non-goals and `tasks.md §"Out-of-scope tasks"`. **Recommend follow-up**: a separate change to bring §4 fully in sync with the 12-tool surface.

### Suggestions (informational, non-blocking)

1. **Bring §4 fully in sync with all 12 tools** — separate change to add Steps 6-11 listing the six radio-link tools and the four intervention-memory operators.
2. **Rename stale test function names** for clarity (`..._twelve_tools`).
3. **Strengthen `MutableInventory` thread-safety test surface** — read-through `get(...)` during a concurrent `register(...)` race isn't explicitly tested. Reads lock-free against a `dict` is safe under CPython GIL but could break under free-threading (PEP 703). Flag for a future free-threaded audit.
4. **Back-port `_iter_json_files` walker fix as a regression test** — the fix in `src/nora/drivers/oid_catalog.py:9` to skip `*.source.json` files was required for the re-sign to verify cleanly. Worth a `tests/test_oid_catalog.py::test_iter_json_files_skips_source_files` pin so the walker doesn't drift back.

### Drift Check

- Design alignment: 11/11 design decisions implemented (per `verify-report.md` §"Design alignment").
- Proposal success criteria: 9/9 met (per `verify-report.md` §"Proposal success criteria").
- Explore findings: 4/4 addressed (per `verify-report.md` §"Explore findings").
- Architectural spot-check: 9/9 contracts verified (per `verify-report.md` §"Architectural Spot-Check").

## Source of Truth Updated

The following canonical specs now reflect the new behavior:

- `openspec/specs/nora-mcp-server/spec.md` (18822 bytes; 15 requirements, 38 scenarios)
- `openspec/specs/ad-hoc-device-registration/spec.md` (8251 bytes; 4 requirements, 18 scenarios — NEW capability)

## Archive Contents

- `proposal.md` ✅
- `design.md` ✅
- `tasks.md` ✅ (10/10 work-unit tasks complete; T11 operator-side deferred per apply-progress #13225)
- `verify-report.md` ✅ (PASS verdict; 0 critical findings; 4 WARNINGs carried forward)
- `explore.md` ✅ (preserved from change folder)
- `spec.md` ✅ (original change-root spec.md, preserved for history; 14198 bytes)
- `specs/nora-mcp-server/spec.md` ✅ (per-domain delta used for compose; 7069 bytes)
- `specs/ad-hoc-device-registration/spec.md` ✅ (per-domain delta used to create the new canonical; 8251 bytes)
- `archive-report.md` ✅ (this file; additive-only, excluded from diff readback)

## Issues Closed

- `alexandervazquez98/nora#42` — IPv4-literal resolution gap — **NOT closed by this archive commit**.
  Closes via the PR description's `Closes #42` keyword when the orchestrator merges the
  PR from `feat/register-device-mcp` to `main`. Archive leaves the issue state unchanged.

## GitHub

- Branch: `feat/register-device-mcp`
- PR: `#TBD` (orchestrator creates after archive completes; this sub-agent does NOT push or open PR)
- Commits added by this archive: **1** (`chore(openspec): archive 2026-09-15-register-device-mcp`)
- Branch commits ahead of `origin/main`: **11** (10 implementation + 1 archive)

## Archive Folder Verification

- Active changes directory `openspec/changes/2026-09-15-register-device-mcp/` confirmed **absent**.
- Archive directory `openspec/changes/archive/2026-09-15-register-device-mcp/` confirmed **present** (8 entries: design.md, explore.md, proposal.md, spec.md, specs/, tasks.md, verify-report.md + archive-report.md).
- Mechanical move: `git mv` exit 128 (source was untracked in git index — change folder was never committed in this branch, identical to `2026-09-13-http-sse-transport` archive). Fallback to plain `mv` succeeded per the SKILL's plain-mv fallback path. Pre-move snapshot vs post-move destination: `diff -r` readback **EMPTY (exit 0)**.

### Mechanical Copy Contract (mandatory readback summary)

| Step | Source | Destination | `diff -r` result |
|------|--------|-------------|------------------|
| Compose `nora-mcp-server` | delta `specs/nora-mcp-server/spec.md` (7069 B) | compose-tmp `spec.md.compose-tmp` (18822 B) | n/a — exit 0 of compose tool is the only passing evidence |
| Snapshot | compose-tmp `spec.md.compose-tmp` (18822 B) | snapshot `/tmp/compose-snap-nms/composed.md` (18822 B) | exit 0 (byte-identical) |
| Atomic mv | compose-tmp → canonical | `spec.md` (18822 B) | n/a |
| Readback post-mv | snapshot `/tmp/compose-snap-nms/composed.md` (18822 B) | canonical `spec.md` (18822 B) | exit 0 (empty diff) |
| Mechanical copy `ad-hoc-device-registration` | delta `specs/ad-hoc-device-registration/spec.md` (8251 B) | tmp `/tmp/spec.md.XXXXXX` (8251 B) | exit 0 (byte-identical) |
| Readback post-mv | delta (8251 B) | canonical `spec.md` (8251 B) | exit 0 (empty diff) |
| Archive folder move | snapshot `/var/folders/.../sdd-archive.XXXXXX/source` | `openspec/changes/archive/2026-09-15-register-device-mcp/` | exit 0 (empty diff) |

All seven `diff -r` reads returned exit 0; the compose tool's exit 0 is the primary composition evidence per the SKILL.

## SDD Cycle Complete

The change has been fully planned, implemented, verified, and archived.
Ready for the orchestrator's next phase: push `feat/register-device-mcp` and open the PR (with `Closes #42` in the body).