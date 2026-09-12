# Tasks: Secure-Configuration Spec Re-issue

> **Apply phase intentionally absent.** `src/nora/config.py` and `tests/test_config.py` (17 tests) are intact at HEAD and already cover every ADDED scenario. No code path or test path is touched. The spec phase already wrote `openspec/changes/2026-09-12-secure-config-reissue/specs/secure-configuration/spec.md`; remaining work is compose-merge + verification + archive.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~100 net (50 delta + 50 added to canonical) |
| 400-line budget risk | Low |
| 800-line review budget risk | Low |
| Chained PRs recommended | No |
| Suggested split | Single merge commit + archive move |
| Delivery strategy | ask-on-risk |
| Chain strategy | single-pr |

Decision needed before apply: No
Chained PRs recommended: No
Chain strategy: single-pr
400-line budget risk: Low

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | Compose-merge delta into canonical + archive move | Single PR | `pytest tests/test_config.py -v` | `pytest --tb=short` (full suite, ≥85% coverage gate) | Revert merge commit → canonical returns to 4 requirements; archive move undone |

## Phase 1: Compose Merge

- [x] 1.1 Run `gentle-ai sdd-archive-compose --canonical openspec/specs/secure-configuration/spec.md --delta openspec/changes/2026-09-12-secure-config-reissue/specs/secure-configuration/spec.md --output openspec/specs/secure-configuration/spec.md.compose-tmp`; assert exit 0. **(exit=0; tmp=6085 B)**
- [x] 1.2 `mv openspec/specs/secure-configuration/spec.md.compose-tmp openspec/specs/secure-configuration/spec.md` (atomic replace). **(mv exit=0; tmp removed)**
- [x] 1.3 `wc -c openspec/specs/secure-configuration/spec.md` → expect ~5000 bytes (grew from 3743). **(post=6085 B, 6 reqs; +2342 B, +2 reqs)**

## Phase 2: Verification

- [x] 2.1 Grep `^## Cross-References` in `openspec/specs/secure-configuration/spec.md` → must exist; entry names `nora-mcp-server > Security Boundary — No Secrets in Tool Responses`. **(found at line 119; entry present)**
- [x] 2.2 Grep `^### Requirement: Security Boundary — No Secrets in Tool Responses` in `openspec/specs/nora-mcp-server/spec.md` (read-only) → must exist (it does at line 63). **(confirmed at line 63; entry-name string matches exactly)**
- [x] 2.3 `uv run pytest tests/test_config.py -v` → 17/17 pass. **(17 passed in 0.56s)**
- [x] 2.4 `uv run pytest --tb=short` → full suite green; coverage ≥ 85% (no regression vs. pre-merge baseline). **(280 passed, 2 skipped, 1 warning in 72.06s; warning is `python -m nora` deprecation notice, not test failure)**
- [x] 2.5 `uv run ruff check .` → exit 0. **(All checks passed!)**
- [x] 2.6 `uv run ruff format --check .` → exit 0. **(60 files already formatted)**
- [x] 2.7 `uv run mypy --strict src/nora` → exit 0. **(Success: no issues found in 26 source files)**
- [x] 2.8 Diff canonical scenarios vs. `nora-mcp-server/spec.md` (read-only) → no scenario duplicated across the two specs (Cross-References pins the boundary). **(0 duplicates; canonical has 15 scenarios, nora-mcp-server has 15; "Security Boundary — No Secrets in Tool Responses" is referenced via Cross-References in canonical, NOT redefined as a Requirement — `grep -c "^### Requirement: Security Boundary" openspec/specs/secure-configuration/spec.md` returns 0)**

## Phase 3: Archive

- [x] 3.1 `cp -R openspec/changes/2026-09-12-secure-config-reissue $TMPDIR/sdd-archive.XXXXXX/source` (read-only snapshot for verification). **(`git mv` refused because source was untracked → fell back to plain `mv` per the skill's safety checks; snapshot lives in `$TMPDIR/sdd-archive.XXXXXX/source`, auto-cleaned by EXIT trap — preferred over `openspec/changes/archive/2026-09-12-secure-config-reissue.snapshot` to avoid leaving stray state in `openspec/`)**
- [x] 3.2 `git mv openspec/changes/2026-09-12-secure-config-reissue openspec/changes/archive/2026-09-12-secure-config-reissue/` (history-preserving move). **(`git mv` exited with `fatal: source directory is empty, source=…, destination=…` because the change folder was untracked; plain `mv` fallback ran the skill's safety checks — snapshot-vs-source diff returned empty, destination absent before move, `mv` exit 0)**
- [x] 3.3 `diff -r $TMPDIR/sdd-archive.XXXXXX/source openspec/changes/archive/2026-09-12-secure-config-reissue` → empty diff required. **(empty diff returned; exit 0; verbatim output captured in archive-report.md §4)**
- [x] 3.4 `rm -rf openspec/changes/archive/2026-09-12-secure-config-reissue.snapshot` (cleanup). **(N/A — snapshot lived in `$TMPDIR` and was auto-cleaned by EXIT trap; no `openspec/`-local snapshot to remove)**
- [x] 3.5 Write `openspec/changes/archive/2026-09-12-secure-config-reissue/archive-report.md`. **(written; full gates summary + diff -r readback + boundary-pinning validation)**
- [x] 3.6 Save engram observation under topic_key `sdd/2026-09-12-secure-config-reissue/archive-report` mirroring the report. **(saved; observation id 13102; sync_id obs-b016440cd2523fba; type=architecture; capture_prompt=false)**

## Exit Signals (Definition of Done)

- Canonical carries 6 requirements + `## Cross-References`.
- `pytest tests/test_config.py` → 17/17; full suite ≥85% coverage.
- `ruff check`, `ruff format --check`, `mypy --strict src/nora` → all exit 0.
- Change folder lives under `openspec/changes/archive/`; `diff -r` snapshot vs. destination is empty.
- Archive report written (file + engram).
