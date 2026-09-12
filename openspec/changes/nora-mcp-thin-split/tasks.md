# Tasks: nora-mcp-thin-split

> Strict-TDD breakdown. Ordering follows `design.md` §11.

## Review Workload Forecast

~1,700 changed lines (≈1,500 deletions + 200 additions). 400-line budget risk: High. Chained PRs recommended: No (locked decision 4). Chain strategy: size-exception. Delivery strategy: ask-on-risk.

Decision needed before apply: Yes
Chained PRs recommended: No
Chain strategy: size-exception
400-line budget risk: High

Single-PR work unit (size:exception). Focused test: `uv run pytest tests/test_no_llm_journal_imports.py tests/test_config.py tests/test_server.py tests/test_integration_boot.py -q`. Runtime harness: `nora-mcp` + `python -m nora` via `tests/test_integration_boot.py`. Rollback: `git revert` merge (orphan NDJSON non-destructive).

## Group 0 — AST guard

- [ ] 0.1 Add `tests/test_no_llm_journal_imports.py` (RED) — AST scan over `src/nora/server.py` + `src/nora/__main__.py` rejecting `nora.llm` / `nora.core.session_journal` / `nora.core.session_*`. RED today; GREEN after 1.2, 2.2, 4.1–4.4.

## Group 1 — Settings trim + dep drop

- [ ] 1.1 Add `tests/test_config.py::test_settings_has_seven_user_fields` (RED) — `len(Settings.model_fields) == 8`; 9 forbidden names absent.
- [ ] 1.2 Trim `src/nora/config.py` (GREEN) — DELETE 9 LLM/journal fields, `ProviderName`, `_DEFAULT_OPERATOR_ALIAS`, `_check_provider_credential`; rewrite `__all__`.
- [ ] 1.3 Regenerate `.env.example` — ≤20 lines, 7 keys. Verify `wc -l .env.example` ≤ 20.
- [ ] 1.4 Add `tests/test_config.py::test_pyproject_has_no_llm_sdk_deps` (RED) then drop `lmstudio>=1,<2` + `google-genai>=1,<3` from `pyproject.toml:24-31` (GREEN); run `uv lock`.
- [ ] 1.5 Update `tests/test_config.py::test_no_os_environ_in_src_nora` allow-list — add `cli.py`.

## Group 2 — Tool removal + server.py trim

- [ ] 2.1 Add `tests/test_server.py::test_server_exposes_exactly_four_tools` (RED) — names == {driver + 3 intervention}.
- [ ] 2.2 Strip `src/nora/server.py` (GREEN) — DELETE `nora_health` + 4 `nora_session_*` + `nora_health_impl` + `_HEALTH_PROBE_SYSTEM` + `_state_to_payload` + `_current_provider` + `_AutoTraceMiddleware` + `_derive_summary` + LLM/journal imports. Rewrite `set_runtime_state(settings)` / `get_runtime_state() -> Settings`. Rewrite 3 intervention tool bodies. Trim `__all__`.
- [ ] 2.3 Add `tests/test_server.py::test_thin_middleware_emits_one_log_line_per_call` (RED).
- [ ] 2.4 Replace `_AutoTraceMiddleware` with `_ToolLogMiddleware` (~15L, GREEN) — `Middleware` subclass emitting one `logger.info` per call. NO journal.
- [ ] 2.5 Update `tests/test_server.py` — DELETE 5 `nora_health` tests + `_call_nora_health`; RENAME nine-tools → four-tools; REWRITE boot-smoke (no `build_provider`); UPDATE 3 `set_runtime_state(settings, provider)` → single-arg.
- [ ] 2.6 Update `tests/conftest.py::hermetic_settings` + `tests/test_server_driver_tool.py::_wired_driver_env` — DELETE 4 journal kwargs + `_reset_llm_factory_cache` autouse.
- [ ] 2.7 DELETE 13 journal-flavoured test files (`tests/test_{session_journal_airgap,server_auto_trace,server_session_tools,llm}.py` + `tests/core/*`). Verify `grep -r "session_journal\|nora.llm" tests/` clean.

## Group 3 — Driver seam removal

- [ ] 3.1 Add `tests/test_driver_snmp_pmp450i.py::test_fetch_radio_metrics_does_not_call_session_journal` (RED) — AST + runtime.
- [ ] 3.2 Cut seam in `src/nora/drivers/snmp_pmp450i/driver.py` (GREEN) — DELETE `_default_set_focus` (L37-52), alias (L55-57), call (L115), docstring step; rewrite `__all__`.
- [ ] 3.3 Remove re-export from `src/nora/drivers/snmp_pmp450i/__init__.py` (L34, L59).
- [ ] 3.4 Update 4 driver tests — DELETE `mock.patch("...nora_session_set_focus", ...)` in `tests/test_driver_snmpsim_v2c.py:234`, `tests/test_driver_snmpsim_v3.py:183`, `tests/test_driver_airgap.py:209`, `tests/test_driver_snmp_pmp450i.py:300`; DELETE `_stub_set_focus` autouse (L35-52).

## Group 4 — Module deletions

- [ ] 4.1 `git rm src/nora/llm.py` (261L).
- [ ] 4.2 `git rm src/nora/core/session_journal.py` (567L).
- [ ] 4.3 `git rm src/nora/core/{session_models,session_paths,session_redaction,session_rotation}.py` (359L).
- [ ] 4.4 `git rm src/nora/core/__init__.py`.

## Group 5 — Entry point split

- [ ] 5.1 Add `tests/test_cli.py::test_cli_module_exists_and_exports_main` (RED).
- [ ] 5.2 Create `src/nora/cli.py` (~30L, GREEN) — sets `FASTMCP_SHOW_SERVER_BANNER=false`; `main()` wires `configure_logging → Settings → set_runtime_state(settings) → PromptRegistry → OidCatalogRegistry.verify_all → Inventory.from_yaml → set_driver(Pmp450iDriver(...)) → mcp.run(show_banner=False)`.
- [ ] 5.3 Replace `src/nora/__main__.py` with 12-line deprecation alias — `warnings.warn("`python -m nora` is deprecated and will be removed in the next minor release. Use `nora-mcp` instead.", DeprecationWarning, stacklevel=2)` + `from nora.cli import main`.
- [ ] 5.4 Update `pyproject.toml:34` — add `nora-mcp = "nora.cli:main"`; keep `nora = "nora.__main__:main"`.
- [ ] 5.5 Add `tests/test_main_alias.py::test_python_dash_m_nora_emits_deprecation_warning` — subprocess `python -m nora`; assert stderr contains `DeprecationWarning` + `will be removed in the next minor release`.

## Group 6 — Integration verification

- [ ] 6.1 `uv run python -m pytest --cov=src/nora --cov-report=term-missing -q` — all green.
- [ ] 6.2 `uv run mypy --strict src/nora` — zero errors.
- [ ] 6.3 `uv run ruff check .` + `uv run ruff format --check .` — zero violations.
- [ ] 6.4 Add `tests/test_integration_boot.py` with `test_subprocess_nora_mcp_exposes_four_tools` + `test_subprocess_python_dash_m_nora_exposes_same_tools` — boot each entry point, JSON-RPC `initialize` + `tools/list`; assert 4-tool set in same order + `DeprecationWarning` on stderr for the alias.

## Cross-cutting guards + forecast

- [ ] CC.1 `tests/intervention_memory/test_no_writes.py` stays green throughout.
- [ ] CC.2 `tests/test_no_llm_journal_imports.py` stays green from 0.1 to end.
- [ ] CC.3 Append deprecation note to `SCOPE.md`; `grep -q "nora-mcp" SCOPE.md` succeeds.

Total: 33 tasks (target 25-35). ~1,700 changed lines → size:exception required (~4× 400-line budget). Single PR per locked decision 4. Risks: `python -m nora` consumer breakage (5.5); mypy strict dead imports (6.2); missed monkeypatches (3.4); `.env.example` creep (1.3); tool-list drift (0.1 + 2.1). Ready for `sdd-apply` after orchestrator confirms `size:exception`.