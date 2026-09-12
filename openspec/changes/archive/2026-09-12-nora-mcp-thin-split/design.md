# Design: nora-mcp-thin-split

## Approach

Single PR. Drop `llm.py`, `core/*`, 9 of 16 `Settings` fields, 2 SDK deps, 5 of 9 `@mcp.tool`s, `_AutoTraceMiddleware`, `_current_provider`, LLM/journal imports, and the `nora_session_set_focus` driver seam. Add `src/nora/cli.py` (~30 lines) and a 12-line `src/nora/__main__.py` deprecation alias. Rename console script `nora` → `nora-mcp`.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Entry-point shape | `cli.py` + alias `__main__.py` | Lock-decision 6 forbids `nora-service`. Sub-module needs no wheel target change. |
| `set_runtime_state` signature | `set_runtime_state(settings)`; `get_runtime_state() -> Settings` | `_current_provider` has zero surviving callers after `nora_health` is dropped (lock-decision 3). |
| `nora_session_set_focus` seam | Drop entirely (no stub, no log) | Lock-decision 5. Simplest cut; explore option (a). No consumer asserts the side-effect. |
| Driver instantiation | Eager at boot | Preserves current behaviour; lazy risks first-call latency. |
| Per-tool observability | Inline `logger.info("tool=%s duration_ms=%d outcome=%s", ...)` per tool body | Spec L93-101 requires one structured stderr line per invocation. See Open Questions. |

## Data Flow

`.env + process env → Settings() → PromptRegistry.from_settings → OidCatalogRegistry.verify_all → Inventory.from_yaml → set_driver(Pmp450iDriver(...)) → mcp.run() → 4 tools over stdio JSON-RPC.`

## server.py diff (current 481 lines)

| Lines | Action | Symbol |
|---|---|---|
| L20-21 | DELETE | `import time`; `from datetime import datetime, timezone` |
| L25 | DELETE | `from fastmcp.server.middleware import Middleware` |
| L29-34 | DELETE | `from nora.core.session_journal import (...)` block |
| L37 | DELETE | `from nora.llm import LLMProvider` |
| L46 | DELETE | `_current_provider: LLMProvider \| None = None` |
| L71-88 | REWRITE | `set_runtime_state(settings)` + `get_runtime_state() -> Settings` |
| L96-142 | DELETE | `_HEALTH_PROBE_SYSTEM`, `nora_health_impl`, `@mcp.tool nora_health` |
| L153-158 | DELETE | `_state_to_payload` helper |
| L161-212 | DELETE | four `@mcp.tool nora_session_*` blocks |
| L283, L310, L339 | REWRITE | `settings, _ = get_runtime_state()` → `settings = get_runtime_state()` |
| L349-464 | DELETE | `_AutoTraceMiddleware`, `_derive_summary`, `_auto_trace_registered`, `register_auto_trace_middleware`, `unregister_auto_trace_middleware` |
| L467-481 | REWRITE | `__all__` to `["mcp","configure_logging","set_runtime_state","get_runtime_state","search_intervention_history","get_device_lifecycle_summary","correlate_sector_interference","snmp_get_pmp450i_radio_metrics"]` |

**KEEP:** L42 `mcp = FastMCP("nora")`; L45 `_current_settings`; L47 `_sanitizer = Sanitizer()` (still bound at L291, L314, L345); L50-68 `configure_logging`; L225-238 `snmp_get_pmp450i_radio_metrics`; L256-292 `search_intervention_history`; L295-315 `get_device_lifecycle_summary`; L318-346 `correlate_sector_interference`.

## config.py diff (current 196 lines)

| Lines | Action | Symbol |
|---|---|---|
| L22 | REWRITE | `from typing import Any` (drop `Literal`) |
| L27, L35 | DELETE | `ProviderName`; `_DEFAULT_OPERATOR_ALIAS` |
| L52-56 | DELETE | LLM block (5 fields) |
| L61-71 | DELETE | SessionJournal block (4 fields) |
| L167-172 | DELETE | `_check_provider_credential` |
| L196 | REWRITE | `__all__ = ["Settings", "LoadSource"]` |

**KEEP:** `LoadSource` (still typed by L102); L43-50 `model_config`; L76-100 driver + intervention fields; L102 `loaded_from`; L104-131 `_detect_loaded_from`; L133-165 `__init__`; L174-193 `settings_customise_sources`.

## pyproject.toml diff

| Lines | Action | Change |
|---|---|---|
| L26-27 | DELETE | `lmstudio>=1,<2` and `google-genai>=1,<3` |
| L34 | REWRITE | Keep `nora = "nora.__main__:main"` (alias body) |
| L34+ | ADD | `nora-mcp = "nora.cli:main"` |

`[tool.hatch.build.targets.wheel] packages = ["src/nora"]` (L48-49) unchanged.

## Driver seam

`src/nora/drivers/snmp_pmp450i/driver.py`: DELETE L35-52 `_default_set_focus` (with L44 lazy `get_journal` import); DELETE L55-57 `nora_session_set_focus` alias; DELETE L105 docstring step; DELETE L115 call site; REWRITE L172 `__all__ = ["Pmp450iDriver", "default_client_factory"]`. `src/nora/drivers/snmp_pmp450i/__init__.py`: DELETE L34 import, L59 `__all__` entry.

## Test migration

**DELETE (13 files):** `tests/test_llm.py`, `tests/test_server_auto_trace.py`, `tests/test_server_session_tools.py`, `tests/test_session_journal_airgap.py`, `tests/core/{__init__.py, conftest.py, test_session_journal.py, test_session_journal_property.py, test_session_models.py, test_session_paths.py, test_session_redaction.py, test_session_rotation.py, test_session_summarize.py}`. Empty `tests/core/` removed.

**NEW:** `tests/test_no_llm_journal_imports.py` — AST scan asserting `server.py` and `__main__.py` import neither `LLMProvider` nor `init_session_journal`.

**MIGRATE (8 files):**

| File | Edits (line citations are in the design log) |
|---|---|
| `tests/conftest.py` | Drop `from nora import llm as llm_mod` + `_reset_llm_factory_cache` autouse + journal kwargs from `hermetic_settings`. |
| `tests/test_config.py` | DELETE 7 tests (LLM-credential, missing-key, defaults, env-precedence using `LMSTUDIO_MODEL_ID`, SessionJournal defaults/override/env-example). UPDATE `test_no_os_environ_in_src_nora` allow-list to include `cli.py`; UPDATE `test_defaults_are_explicit_when_nothing_is_set` + `test_env_file_takes_precedence_over_process_env` to use 7 surviving fields. |
| `tests/test_server.py` | DELETE 5 health tests + `_call_nora_health`. REWRITE `test_main_module_boots_fastmcp_over_stdio` → assert `OidCatalogRegistry.verify_all`. UPDATE 3× `set_runtime_state(settings, provider)` → `set_runtime_state(settings)`. RENAME `test_mcp_instance_exposes_all_nine_tools` → `four_tools`. UPDATE subprocess boot smoke to assert `nora-mcp boot complete` log. ADD `test_alias_emits_deprecation_warning` + `test_alias_and_nora_mcp_expose_identical_tool_lists`. |
| `tests/test_server_driver_tool.py` | Drop journal kwargs from `_wired_driver_env`; drop `_fake_fetch` focus stub; drop provider+journal+`register_auto_trace_middleware`/`unregister_auto_trace_middleware` calls; DELETE 3 journal-asserting tests. ADD `test_driver_does_not_call_session_set_focus`. |
| `tests/test_integration.py` | Drop LLM env pop; UPDATE `_boot_server` integration test names; assert `nora-mcp boot complete` not `active_provider`; DELETE `test_subprocess_uses_configured_provider_via_dotenv`; ADD `_boot_server_via_nora_mcp_script` sibling that drives the `nora-mcp` console script (spec: "alias and nora-mcp expose identical tool lists"). |
| `tests/test_driver_snmpsim_v3.py` | DROP `mock.patch("nora.drivers.snmp_pmp450i.driver.nora_session_set_focus", ...)` |
| `tests/test_driver_snmpsim_v2c.py` | DROP same mock.patch |
| `tests/test_driver_airgap.py` | DROP same mock.patch |
| `tests/test_driver_snmp_pmp450i.py` | DROP `_stub_set_focus` autouse fixture + same mock.patch |

**KEEP unchanged:** `test_toolchain.py`, `test_smoke.py`, `test_prompts.py`, `test_oid_catalog.py`, `test_inventory.py`, `test_driver_exceptions.py`, `test_sanitizer.py`, `test_driver_snmp450i_readonly.py`, `intervention_memory/*` (7 files).

## .env.example (final, 16 lines)

```
# NORA MCP — sanitized runtime configuration template (thin split).
# Copy to `.env` and replace the placeholders before booting NORA.
# NEVER commit `.env`; it is gitignored.

# --- Driver layer (PMP 450i SNMP driver) -----------------------------------
NORA_OID_CATALOGS_PATH=./data/oid-catalogs/
NORA_DEVICES_INVENTORY_PATH=./data/devices.yaml
NORA_OID_CATALOG_SIGNING_KEY=change-me
NORA_PROMPTS_DIR=

# --- Intervention memory MCP (Phase 3) --------------------------------------
NORA_INTERVENTIONS_DIR=./var/interventions/
NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS=1000
NORA_INTERVENTIONS_CORRELATE_SCAN_LIMIT=50
```

## Risks

1. **Observability regression** (spec L93-101). Resolution: inline per-tool `logger.info`. See Open Questions.
2. **No journal NDJSON fixtures** (verified `find`): `var/sessions/` empty; `tests/fixtures/intervention_memory/*.json` unchanged.
3. **`set_runtime_state(settings, provider)` callers** (`test_server.py` L453, L551, L608): collapse to single-arg.
4. **External callers of `nora_health` / `nora_session_*`**: explore L357-358 — zero hits outside `src/`, `tests/`, `openspec/`. PR description surfaces the break for openchat.
5. **Boot order**: `set_runtime_state(settings)` first in `cli.main()` so intervention tools read state before IO.
6. **Fail-closed boot**: `OidCatalogRegistry.verify_all` raises on missing/invalid signing key; `Inventory.from_yaml` raises on missing file. Spec "missing required setting fails fast" satisfied via `nora_oid_catalog_signing_key`.

## Implementation order (strict-TDD)

| Group | Change | RED test |
|---|---|---|
| **1 — Settings trim + dep drop** | DELETE 9 fields + `_check_provider_credential` + `ProviderName` + `_DEFAULT_OPERATOR_ALIAS`; DELETE `_reset_llm_factory_cache` autouse; DELETE `lmstudio`, `google-genai`; DELETE `tests/core/` (13 files); UPDATE `hermetic_settings` + `test_config.py` survivors | `len(Settings.model_fields) == 8` |
| **2 — server.py trim + llm/core delete** | DELETE `llm.py`, `core/`; DELETE 5 tool bodies + middleware + `_current_provider`; REWRITE `set_runtime_state(settings)` / `get_runtime_state() -> Settings`; UPDATE 3 intervention tool bodies; ADD inline tool logging. DELETE 3 server test files | `await mcp.list_tools()` returns 4 names |
| **3 — Driver seam** | DELETE `nora_session_set_focus` from `driver.py` (L35-57, L105, L115) and `__init__.py` (L34, L59); UPDATE 4 driver tests | `test_driver_does_not_call_session_set_focus` |
| **4 — Entry point split** | CREATE `src/nora/cli.py`; REWRITE `src/nora/__main__.py` (12L); ADD `nora-mcp` script; REWRITE `.env.example`; UPDATE `test_server.py` boot smoke + `test_integration.py` (boot via both entry points); UPDATE `test_no_os_environ_in_src_nora` allow-list | Deprecation warning + identical 4-tool surface from both scripts |

## Testing

| Layer | What | Where |
|---|---|---|
| Unit | Settings field count = 8; tool list = 4; tool log line shape | `test_config.py`, `test_server.py` |
| Integration | `python -m nora` and `nora-mcp` expose identical tool lists; DeprecationWarning emitted | `test_integration.py` subprocess |
| E2E | `OidCatalogRegistry.verify_all` boots hermetic; full `Client(mcp)` roundtrip | `test_server.py`, `test_server_driver_tool.py` |

Gates: `pytest --cov=src/nora`; `ruff check .`; `ruff format --check .`; `mypy --strict src/nora`.

## Threat Matrix

N/A — no routing/shell/subprocess/VCS/PR/executable/process-integration boundary changes.

## Migration / Rollout

No data migration. Operators with old LLM/journal env vars see Pydantic `extra="ignore"` (`config.py` L49) silently ignore them. PR description flags the 5 removed tools as breaking for openchat.

## Open Questions

- [ ] Confirm per-tool inline `logger.info("tool=... duration_ms=... outcome=...")` in the 4 surviving tool bodies satisfies spec MODIFIED Requirement "Observability — Stderr Tool Diagnostics" (`specs/nora-mcp-server/spec.md:93-101`). Alternative: thin log-only FastMCP middleware.
