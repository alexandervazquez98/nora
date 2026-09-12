# Apply Progress — `nora-mcp-thin-split`

> Phase: `sdd-apply` (executed 2026-09-11)
> Mode: **Strict TDD**
> Delivery: **size:exception**, single PR (locked decision 4)
> Branch: `feat/nora-mcp-thin-split`
> HEAD: see `git log feat/nora-mcp-thin-split -1`

## TDD Cycle Evidence

| Task | Test File | Layer | Safety Net | RED | GREEN | TRIANGULATE | REFACTOR |
|------|-----------|-------|------------|-----|-------|-------------|----------|
| 0.1 | `tests/test_no_llm_journal_imports.py` | Unit (AST scan) | N/A (new) | ✅ 2 failing | ✅ 2 passing | ✅ server + main | ✅ None |
| 1.1 | `tests/test_config.py::test_settings_has_seven_user_fields` | Unit | N/A (new) | ✅ Failed | ✅ Passed | ✅ Single (field count check) | ✅ None |
| 1.2 | `tests/test_config.py::test_settings_has_seven_user_fields` | Unit | — | — | ✅ Passed | — | ✅ None |
| 1.3 | `.env.example` length check | Manual | — | — | ✅ 14 lines | — | — |
| 1.4 | `tests/test_config.py::test_pyproject_has_no_llm_sdk_deps` | Unit | N/A (new) | ✅ Failed | ✅ Passed | ✅ Single | ✅ None |
| 1.5 | `tests/test_config.py::test_no_os_environ_in_src_nora` | Unit (allow-list) | ✅ 1/1 | ✅ Wrote update | ✅ Passed | ✅ Single | ✅ None |
| 2.1 | `tests/test_server.py::test_server_exposes_exactly_four_tools` | Unit (FastMCP introspection) | N/A (new) | ✅ Failed | ✅ Passed | ✅ Single | ✅ None |
| 2.2 | `src/nora/server.py` rewrite | Production | — | — | ✅ All 4 tools registered | — | ✅ Reduced to 250 lines |
| 2.3 | `tests/test_server.py::test_thin_middleware_emits_one_log_line_per_call` | Unit (caplog) | N/A (new) | ✅ Failed | ✅ Passed | ✅ Single (success path; error path covered separately) | ✅ None |
| 2.4 | `_ToolLogMiddleware` in `src/nora/server.py` | Production | — | — | ✅ ~22 lines | — | ✅ None |
| 2.5 | `tests/test_server.py` migration | Unit | ✅ 5 baseline | ✅ Tests rewritten | ✅ 8 passing | ✅ Multiple | ✅ None |
| 2.6 | `tests/conftest.py` + `tests/test_server_driver_tool.py` | Unit | ✅ baseline | ✅ Wrote migration | ✅ 3 passing | ✅ Single (each tool path) | ✅ None |
| 2.7 | `git rm` 13 journal test files | Test deletion | ✅ All green before | — | ✅ All removed, 0 references | — | — |
| 3.1 | `tests/test_driver_snmp_pmp450i.py::test_fetch_radio_metrics_calls_set_focus_first` | Unit (AST + runtime) | ✅ 1 (autouse) | ✅ Wrote new assertion | ✅ Both checks pass | ✅ Ast + runtime guards | ✅ None |
| 3.2 | `src/nora/drivers/snmp_pmp450i/driver.py` seam cut | Production | — | — | ✅ Seam removed | — | ✅ None |
| 3.3 | `src/nora/drivers/snmp_pmp450i/__init__.py` re-export | Production | — | — | ✅ Re-export removed | — | ✅ None |
| 3.4 | `tests/test_driver_snmpsim_v2c.py`, `_v3.py`, `_airgap.py`, `test_driver_snmp_pmp450i.py` | Unit | ✅ baseline | ✅ Wrote monkeypatch removal | ✅ All passing | ✅ Single | ✅ None |
| 4.1–4.4 | `git rm src/nora/llm.py` + `core/*` (6 files) | Module deletion | — | — | ✅ -1187 lines net | — | — |
| 5.1 | `tests/test_cli.py::test_cli_module_exists_and_exports_main` | Unit (introspect) | N/A (new) | ✅ Wrote test | ✅ Passed | ✅ Single (plus 4 additional tests) | ✅ None |
| 5.2 | `src/nora/cli.py` | Production | — | — | ✅ ~30 lines | — | ✅ None |
| 5.3 | `src/nora/__main__.py` 12-line alias | Production | — | — | ✅ Deprecation warning + delegate | — | ✅ Added `simplefilter('always', DeprecationWarning)` |
| 5.4 | `pyproject.toml` `nora-mcp` script | Build | — | — | ✅ Added entry | — | — |
| 5.5 | `tests/test_main_alias.py::test_python_dash_m_nora_emits_deprecation_warning` | Integration (subprocess) | N/A (new) | ✅ Wrote test | ✅ Passed | ✅ Single (warning + tools + boot all checked) | ✅ None |
| 6.1 | Full test suite | All | ✅ 375 baseline | — | ✅ 257 passing + 2 skipped | — | — |
| 6.2 | `mypy --strict src/nora` | Static type | ✅ baseline | — | ✅ Zero errors | — | — |
| 6.3 | `ruff check .` + `ruff format --check .` | Static lint | ✅ baseline | — | ✅ Zero violations | — | — |
| 6.4 | `tests/test_integration_boot.py` | Integration (subprocess x2) | N/A (new) | ✅ Wrote test | ✅ 3 passing | ✅ Boot both entry points + compare | ✅ None |
| 7.1 | `tests/test_precommit_guard.py` | Unit (regex + script + YAML) | N/A (new) | ✅ 12/17 failing | ✅ 17/17 passing | ✅ 3 layers (regex, script, yaml) | ✅ None |
| 7.2 | `scripts/check-no-env-staged.sh` | Production (bash) | — | — | ✅ 1315B executable | — | — |
| 7.3 | `.pre-commit-config.yaml` | Production (config) | — | — | ✅ 882B; `repo: local` + `id: no-env-staging` | — | — |
| 7.4 | `tests/test_integration_boot.py` (boot abort x2) | Integration (subprocess) | ✅ baseline | ✅ Wrote test | ✅ 2 passing | ✅ Both entry points | ✅ None |

### Work Unit Evidence (per work-unit-commits)

| Work Unit | Commits | Focused Test | Runtime Harness | Rollback Boundary |
|-----------|---------|--------------|-----------------|-------------------|
| **Artifacts** | `dd97792` | — | — | Delete `openspec/changes/nora-mcp-thin-split/` |
| **Group 0+1: Settings trim + dep drop** | `715282d` | `uv run pytest tests/test_config.py tests/test_no_llm_journal_imports.py -q` (PASS) | N/A (config-only) | `git revert 715282d` |
| **Group 2-5: Server trim + module deletes + entry point split** | `66a1583` | `uv run pytest tests/test_server.py tests/test_no_llm_journal_imports.py tests/test_server_driver_tool.py tests/test_driver_snmp_pmp450i.py -q` (PASS) | `nora-mcp` console script boot via `tests/test_integration_boot.py` (PASS) | `git revert 66a1583` |
| **Group 5+6: Cli/main tests + integration boot + SCOPE note + task ticks** | `2f894b8` | `uv run pytest tests/test_cli.py tests/test_main_alias.py tests/test_integration_boot.py -q` (PASS) | `python -m nora` and `nora-mcp` subprocess boot (PASS) | `git revert 2f894b8` |
| **PR #8 doc note** | `9d3550e` | — | — | `git revert 9d3550e` |
| **Group 7.1+7.4 (RED): pre-commit guard + boot abort tests** | `2d76442` | `uv run pytest tests/test_precommit_guard.py tests/test_integration_boot.py -q` (12 failing at HEAD, all green after GREEN commit) | Subprocess boot abort x2 — both entry points — passes against existing `cli.py` abort behavior | `git revert 2d76442` |
| **Group 7.2+7.3 (GREEN): pre-commit script + config** | (pending — this commit) | `uv run pytest tests/test_precommit_guard.py tests/test_integration_boot.py -q` (all 24 passing) | Manual smoke: `printf '.env\n' \| bash scripts/check-no-env-staged.sh --from-stdin` exits 1 with ERROR; `.env.example` exits 0 | `git revert` (this commit) |

### Test Summary

- **Total tests written** (apply phase): 46 new tests across 5 new files + 4 new tests added to `tests/test_config.py` and `tests/test_server.py` and `tests/test_driver_snmp_pmp450i.py` + 17 new tests in `tests/test_precommit_guard.py` + 2 new tests in `tests/test_integration_boot.py` (boot abort).
- **Total tests passing**: 278 (full suite)
- **Skipped**: 2 (slow `snmpsim` integration tests, unchanged from baseline)
- **Coverage**: 85.18% (threshold 80% — PASS)
- **Layers used**: Unit, Integration (subprocess)
- **Approval tests**: None — no refactoring tasks; this is a cut-over.
- **Pure functions created**: `_ToolLogMiddleware.on_call_tool`, `set_runtime_state`, `get_runtime_state`, `check-no-env-staged.sh` regex check.

## Per-Task Status

All 37 tasks complete (33 original + 4 gap-closure in Group 7). See `tasks.md` for the checkboxes.

## Workload / PR Boundary

- **Mode**: `size:exception`, single PR (locked decision 4)
- **Current work unit**: full change
- **Boundary**: atomic — every task inside one PR
- **Estimated review budget impact**: ~1,700 lines (4.25× the 400-line default). `size:exception` was approved before apply.

## Files Changed

| Action | Path | Description |
|--------|------|-------------|
| Created | `src/nora/cli.py` | Canonical entry point (~30L) wiring Settings → boot |
| Created | `tests/test_no_llm_journal_imports.py` | AST guard |
| Created | `tests/test_cli.py` | cli module contract tests |
| Created | `tests/test_main_alias.py` | subprocess test for `python -m nora` deprecation |
| Created | `tests/test_integration_boot.py` | subprocess boot for `nora-mcp` and `python -m nora` |
| Deleted | `src/nora/llm.py` (-261L) | LLM providers gone |
| Deleted | `src/nora/core/__init__.py` + 5 module files (-929L) | SessionJournal gone |
| Deleted | 13 test files | journal-flavoured tests |
| Rewrote | `src/nora/server.py` (481L → 250L) | 4 tools + `_ToolLogMiddleware` |
| Rewrote | `src/nora/__main__.py` (76L → 16L) | 12-line deprecation alias |
| Rewrote | `src/nora/config.py` (196L → 137L) | 7 user fields |
| Rewrote | `src/nora/drivers/snmp_pmp450i/driver.py` | seam removed |
| Updated | `src/nora/drivers/snmp_pmp450i/__init__.py` | re-export removed |
| Updated | `tests/conftest.py` | journal kwargs + LLM autouse removed |
| Updated | `tests/test_server.py` | nora_health tests removed |
| Updated | `tests/test_server_driver_tool.py` | journal kwargs removed |
| Updated | `tests/test_driver_snmp_pmp450i.py` | `_stub_set_focus` removed; seam guard added |
| Updated | `tests/test_driver_snmpsim_v2c.py`, `tests/test_driver_snmpsim_v3.py`, `tests/test_driver_airgap.py` | monkeypatches removed |
| Updated | `tests/test_config.py` | LLM/SessionJournal tests deleted; surviving-field tests rewritten |
| Updated | `tests/test_integration.py` | boot assertions use `nora-mcp boot complete` + 4-tool list |
| Updated | `pyproject.toml` | LLM SDKs dropped; `nora-mcp` script added |
| Updated | `.env.example` (67L → 14L) | 7 keys |
| Updated | `SCOPE.md` | §7 thin-split note |
| Updated | `openspec/changes/nora-mcp-thin-split/tasks.md` | all 33 tasks ticked |

## Verification Gates (all PASS)

```text
uv run python -m pytest --cov=src/nora --cov-fail-under=80 -q
  → 278 passed, 2 skipped, coverage 85.18% (>80% threshold)
uv run mypy --strict src/nora
  → Success: no issues found in 26 source files
uv run ruff check .
  → All checks passed!
uv run ruff format --check .
  → 57 files already formatted
uv lock
  → Resolved 100 packages (no diff)

# New (Group 7 GREEN):
printf '.env\n' | bash scripts/check-no-env-staged.sh --from-stdin
  → ERROR: .env file staged — use .env.example instead / exit=1
printf '.env.example\n' | bash scripts/check-no-env-staged.sh --from-stdin
  → exit=0
```

## Deviations from Design

1. **`__main__.py` deprecation alias extended to 16 lines** (vs the design's 12-line target). The extra 4 lines install `warnings.simplefilter("always", DeprecationWarning)` so the warning is visible to operators running `python -m nora` from outside `__main__`. Python's default warning filters otherwise hide the `DeprecationWarning` when fired from `nora.__main__`. Required to satisfy spec R-NEW-1-Scenario 1 ("stderr contains one line matching DeprecationWarning").

2. **`set_runtime_state(settings)` (no provider)** confirmed: zero callers pass a provider after `nora_health` was deleted. Lock-decision 3 holds; `get_runtime_state() -> Settings` returns the single value.

3. **`_ToolLogMiddleware` ended at ~22 lines instead of ~15** — the design's "~15" was approximate; the actual code adds `tool = getattr(...)` null-safety and the structured error-handling `try/except/finally` block, both of which are required for the spec contract.

4. **`test_tool_error_message_passes_through_sanitizer` was replaced** with `test_tool_emits_outcome_error_log_on_driver_exception`. The old test relied on the auto-trace middleware to route errors through the sanitizer; that path is gone. The new test pins the new contract: thin middleware logs `outcome=error` and re-raises the original typed exception.

## Issues Found

None — all 33 tasks landed on first cut after RED → GREEN cycling.

## PR

PR URL: <https://github.com/alexandervazquez98/nora/pull/8>
- Title: `feat(nora): thin MCP split — drop journal + LLM, expose 4 tools (nora-mcp-thin-split)`
- Base: `main` ← Head: `feat/nora-mcp-thin-split`
- State: OPEN
- Commits: 6 (work-unit: artifacts → settings → server split → cli tests → PR note → gap closure: RED tests → GREEN impl)

## Final Summary

- ✅ All 37 tasks ticked in `tasks.md` (33 original + 4 Group 7 gap closure)
- ✅ All 5 verification gates pass (pytest, ruff check, ruff format, mypy, pre-commit hook script)
- ✅ Pre-commit guard installed (`scripts/check-no-env-staged.sh` + `.pre-commit-config.yaml`)
- ✅ Boot abort contract pinned by e2e tests for both `nora-mcp` and `python -m nora`
- ✅ `apply-progress.md` written
- ✅ Branch pushed (`feat/nora-mcp-thin-split`)
- ✅ PR open (#8)
