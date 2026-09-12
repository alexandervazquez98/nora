# Archive Report — phase3-intervention-memory-mcp

> Schema: gentle-ai.archive-result/v1
> archive_status: intentional-with-warnings
> archive_decision: PASS WITH WARNINGS — 0 CRITICAL, 3 WARNING (1 fixed, 2 accepted as follow-ups), 4 SUGGESTION
> accepted_by: orchestrator, with user decision this session

## Purpose

Phase 3 — `phase3-intervention-memory-mcp` — NetOps Persistent Memory & Correlation MCP server integrated with NORA's existing FastMCP. The change ships a new read-only `src/nora/intervention_memory/` package (7 modules, ~530 LOC production) plus three `@mcp.tool` registrations on NORA's global FastMCP instance (`search_intervention_history`, `get_device_lifecycle_summary`, `correlate_sector_interference`) and a `shim_webui.py` mirror for openchat's `webui.db` deploy path. Branch `feat/phase3-intervention-memory-mcp`, HEAD `b5747c0`, 13 commits total (12 work-unit commits from `sdd-apply` plus 1 fix commit for WARNING-3). Verdict PASS WITH WARNINGS — finalize as intentional-with-warnings and move change artifacts to `openspec/changes/archive/2026-09-11-phase3-intervention-memory-mcp/`.

## Verdict

- **Verdict**: PASS WITH WARNINGS (per `verify-report.md` at HEAD `6bf4a9f`, refreshed by WARNING-3 fix commit `b5747c0`).
- **CRITICAL**: 0.
- **WARNING**: 3 total — 1 fixed, 2 accepted as follow-ups (none blocking archive).
- **SUGGESTION**: 4 total — accepted as nice-to-haves, no code change required.
- **Status**: `intentional-with-warnings` (per OpenSpec convention; same shape as `phase2-pmp450i-driver` archive).
- **Accepted by**: orchestrator, with explicit user decision this session.
- **Implementation status**: Mergeable. PR #7 open at <https://github.com/alexandervazquez98/nora/pull/7> awaiting maintainer review.

## Final State

| Field | Value |
|-------|-------|
| Branch | `feat/phase3-intervention-memory-mcp` |
| HEAD | `b5747c0` |
| Total commits in this change | 13 (12 work-unit + 1 WARNING-3 fix) |
| HEAD commit message | `refactor(nora): drop dead _enforce_keyword_cap from intervention_memory.tools` |
| Oldest commit | `c9b1b6d` `feat(nora): scaffold intervention_memory package with AST read-only guard` |
| Pull request | PR #7 — <https://github.com/alexandervazquez98/nora/pull/7> |
| Merge state | NOT merged to `main` (maintainer's responsibility, like prior PR #5 and PR #6) |
| Test count (full suite) | 375 passed, 2 skipped |
| New tests added in this change | 82 (under `tests/intervention_memory/`) + 5 shim tests + 47 wiring/config/auto-trace tests |
| Coverage on `src/nora/intervention_memory/` | 95% line coverage (gate ≥85% per `openspec/config.yaml:116`; R11 forecast ≥88%) |
| `ruff check .` | Clean |
| `ruff format --check .` | Clean |
| `mypy --strict src/nora/` | Clean |
| AST scan (`tests/intervention_memory/test_no_writes.py`) | 6/6 green, including poison self-test |
| `pytest --strict-markers --strict-config` | 375 passed, 2 skipped |

## WARNING Resolution

Per the Final-State Authority hierarchy, the resolutions below honour the orchestrator's explicit launch-prompt facts over `verify-report.md` and `apply-progress` snapshots:

| ID | Source | Description | Resolution | Evidence |
|----|--------|-------------|------------|----------|
| **WARNING-1** | `verify-report.md` (at verification time, HEAD `6bf4a9f`) | Fixture-path doc drift — `design.md:67-71` and `tasks.md:106-110,318` say `tests/intervention_memory/fixtures/...` but actual path is `tests/fixtures/intervention_memory/...`. | **Accepted as follow-up** (documentation only; test code uses the correct path). Not in implementation. Future doc-polish PR. | Per orchestrator launch prompt (final-state authority). |
| **WARNING-2** | `verify-report.md` (at verification time, HEAD `6bf4a9f`) | Risk-count typo — orchestrator prompt said "12 risks" but `proposal.md` risk register contains 11 (R1-R11). The proposal's risk register (`proposal.md:264-278`) lists exactly 11 rows; `grep -c "^| R" proposal.md` returns 11. The orchestrator's launch-prompt description of "13 risks (R1-R11 + R-NEW-1..4)" is itself inconsistent math (11 + 4 = 15, not 13); the proposal and verify-report both confirm 11 risks as the authoritative count. | **Accepted as follow-up** (off-by-one in prompt text; the 11 risks from the proposal are fully verified). Not in implementation. Future doc-polish PR. | Per orchestrator launch prompt + `proposal.md` (the source of truth for the risk register) + `verify-report.md`. |
| **WARNING-3** | `verify-report.md` (at verification time, HEAD `6bf4a9f`) | Dead `_enforce_keyword_cap` function in `tools.py:70-90` (per pre-fix file); exported in `__all__` but zero callers. The cap logic is enforced inline at `tools.py:121-132` (post-fix file). | **FIXED in commit `b5747c0`** — function deleted, removed from `__all__`. All quality gates re-confirmed green after the fix. | Per orchestrator launch prompt (final-state authority, outranks verify-report's pre-fix claim of "dead code exists"). The fix commit message: `refactor(nora): drop dead _enforce_keyword_cap from intervention_memory.tools`. |

### SUGGESTIONs (accepted as nice-to-haves, no archive-blocking)

- **SUGGESTION-1**: `correlate_sector_interference` builds the conflict dict manually instead of routing through `sanitize_record_payload`. Code is correct; mechanism is fragile. Recommendation: either document or route through a sanitization helper that explicitly carries the bypass. Keep current behaviour.
- **SUGGESTION-2**: `_pick_offline_subscribers` builds subscriber dicts via `model_dump(mode="json")` then sanitizes each field individually. Two sanitization paths coexist. Recommendation: refactor to a synthetic `PreExistingOfflineSubscriber` Pydantic model. Keep current behaviour.
- **SUGGESTION-3**: `test_correlate_result_neighbor_sanitized` includes a dead `or` branch (`TWR-ISABEL-5GHZ-A` is always matched by `SERIAL_REGEX` and replaced). Recommendation: drop the `or` branch. Test still passes; just harder to read.
- **SUGGESTION-4**: Orchestrator prompt and `tasks.md:255,259` reference `tests/intervention_memory/test_shim_webui.py` which does not exist; the five shim tests live in `test_tools.py::test_shim_*`. Doc-drift only.

## Spec Compliance (final state)

All requirements and scenarios are verified compliant per `verify-report.md` (intermediate snapshot, carried forward as the post-fix state matches the snapshot's content):

| Capability | Requirements | Scenarios | Status |
|---|---|---|---|
| `intervention-memory` (R1–R11) | 11 | 33 | All COMPLIANT |
| `nora-mcp-server` (R-NEW-1..4) | 4 | 10 | All COMPLIANT |
| **Total** | **15** | **43** | **All COMPLIANT** |

Coverage: 95% line coverage on `src/nora/intervention_memory/` (gate ≥85%).

## Spec Deltas Applied

Two spec operations are performed at archive time:

1. **New capability `intervention-memory`**: full spec `openspec/changes/phase3-intervention-memory-mcp/specs/intervention-memory/spec.md` is a complete capability (not a delta), so it is mechanically copied to `openspec/specs/intervention-memory/spec.md`. Follows Step 2 "If Main Spec Does NOT Exist" path with `cp` + `diff -r` readback.
2. **Delta to `nora-mcp-server`**: delta spec `openspec/changes/phase3-intervention-memory-mcp/specs/nora-mcp-server/spec.md` (R-NEW-1..R-NEW-4, 10 ADDED Scenarios) is composed into the existing `openspec/specs/nora-mcp-server/spec.md` via `gentle-ai sdd-archive-compose`. The `#### ADDED Scenario:` markers are preserved per OpenSpec convention so future readers see the delta, not a fresh write. A "Depends on" cross-reference is appended after composition.

No requirements are renumbered. No existing `nora-mcp-server` requirement is modified or removed.

## Artifacts Archived

Final inventory under the change directory (verified present at archive time):

| Path | Status | Notes |
|---|---|---|
| `explore.md` | Present | Sourced from `sdd-explore` |
| `proposal.md` | Present | 11 risks (R1–R11) per proposal risk register |
| `design.md` | Present | Has the fixture-path doc drift (WARNING-1) |
| `tasks.md` | Present | 26 tasks, all marked `[x]` per `sdd-apply` |
| `verify-report.md` | Present | Generated by `sdd-verify` at HEAD `6bf4a9f` |
| `specs/intervention-memory/spec.md` | Present | New full capability spec |
| `specs/nora-mcp-server/spec.md` | Present | Delta spec with `#### ADDED Scenario:` markers |
| `archive-report.md` | Present (this file) | Generated by `sdd-archive` |

After archive, the change tree moves to `openspec/changes/archive/2026-09-11-phase3-intervention-memory-mcp/` with `diff -r` byte-identity verification against a pre-move snapshot.

## Carry-Forward Risks for Future Slices

- **WARNING-1 doc polish** — Update `design.md:67-71` and `tasks.md:106-110,318` to reference `tests/fixtures/intervention_memory/` (the actual path). Should be addressed in a documentation-polish PR.
- **WARNING-2 doc polish** — Reconcile the risk-count copy in any future orchestrator prompts or references to the proposal. The proposal.md itself is correct (11 risks).
- **SUGGESTION-1 / SUGGESTION-2** — Optional refactor: route `correlate_sector_interference` and `_pick_offline_subscribers` through a single sanitization helper. Not blocking; current behaviour is correct and the user-visible contract is met.
- **SUGGESTION-3** — Optional test cleanup: drop the dead `or` branch in `test_correlate_result_neighbor_sanitized`.
- **SUGGESTION-4** — Drop the stale `tests/intervention_memory/test_shim_webui.py` reference from `tasks.md:73,255,259`.
- **Openchat deploy script** — The `webui.db` registration that consumes `inspect.getsource(Tools)` is openchat's responsibility, NOT NORA's. The next openchat deploy (out of repo) pastes the rendered shim into `webui.db`. Until that lands, the three tools are reachable only via NORA's stdio MCP surface.
- **Production path wiring** — Operator MUST wire the real `.22` path via `NORA_INTERVENTIONS_DIR` env var before deploying. The `.env.example` default is `./var/interventions/` (relative); no real production path enters the repo.

## Hand-Off Notes for Next Slice

- The new `src/nora/intervention_memory/` package is a clean dependency target. Phase 4 or future MCP clients can import from it without coupling to NORA's MCP wiring (the one-way dependency rule is enforced by the AST guard).
- The `webui.db` shim (`shim_webui.py`) is consumed by openchat's deploy script via `inspect.getsource(Tools)`. The openchat side of the integration is NOT NORA's responsibility.
- The `_AutoTraceMiddleware` (existing on `src/nora/server.py`) records every invocation of the three new tools for free, under the existing `session-journal` R2 contract. No middleware change was required.
- The AST no-writes guard at `tests/intervention_memory/test_no_writes.py` is the structural enforcement of the read-only hard rule. Any future contributor who adds a write tool will break the build.

## Verdict Summary

Implementation is mergeable. Verdict PASS WITH WARNINGS — 0 CRITICAL, 3 WARNING (1 fixed in `b5747c0`, 2 accepted as doc-polish follow-ups), 4 SUGGESTION (accepted as nice-to-haves). 15 requirements / 43 scenarios compliant; 95% line coverage on the new package; all quality gates green. PR #7 open at <https://github.com/alexandervazquez98/nora/pull/7> awaiting maintainer review. Archive proceeds under OpenSpec convention `intentional-with-warnings`.
