# Exploration: `nora-mcp-thin-split`

> Architectural split of the NORA monolith into a thin MCP server.
> Date of evidence: branch `main` @ `27a0af6` (Phase 3 already merged).

## Current State

### Module tree under `src/nora/` (verbatim from `glob src/nora/**/*.py`)

| Path | Lines | Imports from elsewhere in `src/nora/` |
|---|---|---|
| `__init__.py` | 7 | — |
| `__main__.py` | 76 | `nora.config`, `nora.drivers.{inventory,oid_catalog,registry,snmp_pmp450i}`, `nora.llm`, `nora.prompts.registry`, `nora.server` |
| `server.py` | 481 | `nora.config`, `nora.core.session_journal`, `nora.drivers`, `nora.intervention_memory.tools`, `nora.llm`, `nora.sanitizer` |
| `config.py` | 196 | — (the `os.environ` boundary) |
| `sanitizer.py` | 203 | — |
| `llm.py` | 261 | `nora.config`, `nora.sanitizer` |
| `core/__init__.py` | 13 | — |
| `core/session_journal.py` | 567 | `nora.config`, `nora.core.{session_models,session_paths,session_redaction,session_rotation}`, `nora.sanitizer` |
| `core/session_models.py` | 67 | — |
| `core/session_paths.py` | 161 | — (stdlib only) |
| `core/session_redaction.py` | 59 | — |
| `core/session_rotation.py` | 72 | `nora.core.session_models` |
| `drivers/__init__.py` | 40 | `nora.drivers.exceptions`, `nora.drivers.registry` |
| `drivers/exceptions.py` | 105 | — |
| `drivers/inventory.py` | 142 | `nora.drivers.exceptions` |
| `drivers/oid_catalog.py` | 186 | `nora.config`, `nora.drivers.exceptions` |
| `drivers/registry.py` | 52 | `nora.drivers.exceptions` (TYPE_CHECKING: `nora.drivers.snmp_pmp450i`) |
| `drivers/snmp_pmp450i/__init__.py` | 60 | `nora.drivers.exceptions`, `nora.drivers.inventory`, `nora.drivers.snmp_pmp450i.{client,driver,report,v2c,v3}` |
| `drivers/snmp_pmp450i/client.py` | 47 | — (Protocol only) |
| `drivers/snmp_pmp450i/driver.py` | 172 | `nora.drivers.{exceptions,inventory,oid_catalog}`, `nora.drivers.snmp_pmp450i.{client,report}`. **Lazy import:** `from nora.core.session_journal import get_journal` (line 44) |
| `drivers/snmp_pmp450i/report.py` | 101 | `nora.drivers.{exceptions,inventory,oid_catalog}` |
| `drivers/snmp_pmp450i/v2c.py` | 116 | `nora.drivers.{exceptions,inventory}` |
| `drivers/snmp_pmp450i/v3.py` | 131 | `nora.drivers.{exceptions,inventory}` |
| `prompts/__init__.py` | 8 | `nora.drivers.exceptions`, `nora.prompts.registry` |
| `prompts/registry.py` | 174 | `nora.config`, `nora.drivers.exceptions` |
| `prompts/snmp_pmp450i.md` | — | (Markdown shipped data) |
| `intervention_memory/__init__.py` | 13 | — |
| `intervention_memory/tools.py` | 315 | `nora.config`, `nora.intervention_memory.{correlation,models,sanitize,storage}`, `nora.sanitizer` |
| `intervention_memory/models.py` | 119 | — |
| `intervention_memory/sanitize.py` | 94 | `nora.intervention_memory.models`, `nora.sanitizer` |
| `intervention_memory/storage.py` | 92 | `nora.intervention_memory.models` |
| `intervention_memory/correlation.py` | 62 | — |
| `intervention_memory/shim_webui.py` | 122 | `nora.config`, `nora.intervention_memory.tools`, `nora.sanitizer` |

### Boot sequence today (verbatim from `src/nora/__main__.py:40-72`)

```
configure_logging()                                  # server.py
Settings()                                           # config.py
provider = build_provider(settings)                  # llm.py
set_runtime_state(settings, provider)                # server.py
init_session_journal(settings)                       # core/session_journal.py
PromptRegistry.from_settings(settings)               # prompts/registry.py
OidCatalogRegistry.verify_all(settings)              # drivers/oid_catalog.py
inventory = Inventory.from_yaml(settings.nora_devices_inventory_path)
set_driver(Pmp450iDriver(inventory=..., catalog_registry=...))
register_auto_trace_middleware()                     # server.py
mcp.run(show_banner=False)                           # server.py — stdio
```

### `@mcp.tool` registrations on the global `mcp` instance (verified by Grep on `src/nora/server.py`)

| Line | Tool | Backing concern | Verdict |
|---|---|---|---|
| 134 | `nora_health` | LLM provider (`provider.complete(...)`) | **ELIMINATE** (locked decision 3) |
| 161 | `nora_session_get_state` | SessionJournal `get_state` | **ELIMINATE** |
| 176 | `nora_session_set_focus` | SessionJournal `set_focus` | **ELIMINATE** |
| 188 | `nora_session_resume` | SessionJournal `resume` | **ELIMINATE** |
| 203 | `nora_session_summarize` | SessionJournal `summarize` | **ELIMINATE** |
| 225 | `snmp_get_pmp450i_radio_metrics` | `Pmp450iDriver.fetch_radio_metrics` | **KEEP** (locked decision 2) |
| 256 | `search_intervention_history` | `intervention_memory.tools.search_intervention_history` | **KEEP** |
| 295 | `get_device_lifecycle_summary` | `intervention_memory.tools.get_device_lifecycle_summary` | **KEEP** |
| 318 | `correlate_sector_interference` | `intervention_memory.tools.correlate_sector_interference` | **KEEP** |

### Settings fields today (verbatim from `src/nora/config.py:43-102`)

Sixteen user-settable fields (`model_fields`): `nora_llm_provider`, `lmstudio_api_host`, `lmstudio_model_id`, `gemini_api_key`, `gemini_model_id`, `nora_session_journal_dir`, `nora_session_trace_max_steps`, `nora_session_journal_enabled`, `nora_operator_alias`, `nora_oid_catalogs_path`, `nora_devices_inventory_path`, `nora_oid_catalog_signing_key`, `nora_prompts_dir`, `nora_interventions_dir`, `nora_interventions_keyword_search_max_records`, `nora_interventions_correlate_scan_limit`. Plus the computed `loaded_from` (not user-settable).

### Dependencies today (`src/nora/pyproject.toml:24-31`)

`fastmcp>=3.2,<4`, **`lmstudio>=1,<2`**, **`google-genai>=1,<3`**, `pydantic-settings>=2`, `puresnmp==2.0.1`, `puresnmp-crypto>=1.0`. Confirmed.

## Affected Areas

The following files / packages change shape:

| Path | Why |
|---|---|
| `src/nora/__main__.py` | Becomes a **deprecation alias** (emits `DeprecationWarning`, delegates to the new entry point). |
| `src/nora/server.py` | Strip 6 of 9 `@mcp.tool` registrations; strip `_AutoTraceMiddleware`, `init_session_journal` re-export, `nora_health_impl`, `LLMProvider` from the boot-time runtime state. The 4 surviving tools (driver + 3 intervention) remain. |
| `src/nora/config.py` | Trim 16 → 7 fields (LLM block gone, SessionJournal block gone). `_check_provider_credential` removed. The `_DEFAULT_OPERATOR_ALIAS` constant + the `ProviderName` / `LoadSource` type aliases lose their callers. |
| `src/nora/llm.py` | **Deleted.** Only caller in production is `nora_health` (`server.py:110`); only callers in tests are `tests/test_llm.py` and `tests/conftest.py:_reset_llm_factory_cache`. |
| `src/nora/core/*` (5 files + `__init__.py`) | **Deleted.** No other production module imports it after the journal/middleware goes. (The only lazy import is `drivers/snmp_pmp450i/driver.py:44` — see "Migration seams" below.) |
| `src/nora/prompts/*` (2 modules + `snmp_pmp450i.md`) | **KEPT.** `PromptRegistry` loads system prompts at boot; no LLM dependency. `nora_prompts_dir` setting survives. |
| `src/nora/drivers/*` (everything except `driver.py`'s journal call) | **KEPT.** Driver is the locked decision 2 surface. One edit: drop the lazy `nora_session_set_focus` call (see "Migration seams"). |
| `src/nora/intervention_memory/*` | **KEPT ENTIRELY.** Read-only package; survives verbatim. |
| `src/nora/sanitizer.py` | **KEPT.** Still used by `server.py` (`_sanitizer = Sanitizer()`, line 47) and by every intervention tool. |
| `src/nora/__init__.py` | **KEPT** as-is (just `__version__`). |
| `pyproject.toml` | Drop `lmstudio>=1,<2` + `google-genai>=1,<3`; rename `nora` console script to a deprecation shim and add `nora-mcp` console script. |
| `.env.example` | Trim 16 keys → 7. |
| `tests/*` (11 files) | See "Test surface mapping" below. |

### Belongs-in-thin classification

| Module / path | Verdict | Justification |
|---|---|---|
| `src/nora/__init__.py` | ✅ KEEP | Version string only; safe. |
| `src/nora/__main__.py` | 🔀 ALIAS | Per locked decision 1: emits `DeprecationWarning`, delegates to the new entry point. Becomes a ~10-line module. |
| `src/nora/server.py` | ✅ KEEP | Hosts `mcp = FastMCP("nora")` + 4 surviving tools + boot-time `_sanitizer`. Body trimmed of LLM/journal/middleware. |
| `src/nora/config.py` | ✅ KEEP | Slimmed to 7 fields. |
| `src/nora/sanitizer.py` | ✅ KEEP | Backs the intervention tool sanitizer boundary + the boot `_sanitizer` singleton. |
| `src/nora/llm.py` | ❌ ELIMINATE | Sole production caller is `nora_health_impl` (`server.py:110`). Locked decision 3. |
| `src/nora/core/__init__.py` | ❌ ELIMINATE | Mark for the SessionJournal package only. |
| `src/nora/core/session_journal.py` | ❌ ELIMINATE | Locked decision 3. |
| `src/nora/core/session_models.py` | ❌ ELIMINATE | Pure Pydantic models for journal steps. |
| `src/nora/core/session_paths.py` | ❌ ELIMINATE | Atomic-write primitive used only by `session_journal.py`. |
| `src/nora/core/session_redaction.py` | ❌ ELIMINATE | R10 frozen list used only by `session_journal.py`. |
| `src/nora/core/session_rotation.py` | ❌ ELIMINATE | NDJSON rotation used only by `session_journal.py`. |
| `src/nora/drivers/__init__.py` | ✅ KEEP | Public surface of the driver package. |
| `src/nora/drivers/exceptions.py` | ✅ KEEP | Typed driver exception hierarchy. |
| `src/nora/drivers/inventory.py` | ✅ KEEP | `Device` + `Inventory` model + YAML loader. |
| `src/nora/drivers/oid_catalog.py` | ✅ KEEP | HMAC-verified OID catalog registry. |
| `src/nora/drivers/registry.py` | ✅ KEEP | Module-level driver singleton. |
| `src/nora/drivers/snmp_pmp450i/__init__.py` | ✅ KEEP | Public facade of the driver subpackage. |
| `src/nora/drivers/snmp_pmp450i/client.py` | ✅ KEEP | `SnmpClient` Protocol. |
| `src/nora/drivers/snmp_pmp450i/driver.py` | ✅ KEEP | One seam to cut: drop `nora_session_set_focus` (line 115). |
| `src/nora/drivers/snmp_pmp450i/report.py` | ✅ KEEP | `RadioMetricsReport` Pydantic model. |
| `src/nora/drivers/snmp_pmp450i/v2c.py` | ✅ KEEP | SNMPv2c wrapper. |
| `src/nora/drivers/snmp_pmp450i/v3.py` | ✅ KEEP | SNMPv3 wrapper. |
| `src/nora/prompts/__init__.py` | ✅ KEEP | Re-export `PromptRegistry`. |
| `src/nora/prompts/registry.py` | ✅ KEEP | One-shot Markdown loader (no LLM dependency). |
| `src/nora/prompts/snmp_pmp450i.md` | ✅ KEEP | Shipped system prompt for the driver tool. |
| `src/nora/intervention_memory/*` (entire package) | ✅ KEEP | Read-only intervention history; locked decision 3 only strips LLM/journal. |
| `src/nora/intervention_memory/shim_webui.py` | ✅ KEEP | openchat deploy mirror — not part of NORA's MCP surface. |

### Settings trimming

Sixteen user-settable fields → **7 survive**. The user-locked "~5" target is approximate; the strict arithmetic after the LLM + journal cut is 7 (4 driver + 1 intervention path + 2 intervention caps). One of these (`nora_prompts_dir`) is an optional override on a path; if the thin split is also a chance to drop the override (the packaged prompts ship in the wheel), the count drops to 6.

**Cut (9 fields):**

| Field | Reason |
|---|---|
| `nora_llm_provider` | Locked decision 3. |
| `lmstudio_api_host` | Only used by `LMStudioProvider`. |
| `lmstudio_model_id` | Same. |
| `gemini_api_key` | Same. |
| `gemini_model_id` | Same. |
| `nora_session_journal_dir` | SessionJournal eliminated. |
| `nora_session_trace_max_steps` | Same. |
| `nora_session_journal_enabled` | Same. |
| `nora_operator_alias` | Same. |

**Keep (7 fields):**

| Field | Default | Why |
|---|---|---|
| `nora_oid_catalogs_path` | `./data/oid-catalogs/` | Driver boot path. |
| `nora_devices_inventory_path` | `./data/devices.yaml` | Driver boot path. |
| `nora_oid_catalog_signing_key` | `None` | HMAC verifier fail-closed. |
| `nora_prompts_dir` | `None` | Optional override; falls back to packaged. |
| `nora_interventions_dir` | `./var/interventions/` | Read-only target. |
| `nora_interventions_keyword_search_max_records` | `1000` | R7 I/O cap. |
| `nora_interventions_correlate_scan_limit` | `50` | R6 scan cap. |

**Proposed `.env.example` for the thin MCP** (verbatim draft, replacing `.env.example:1-67`):

```bash
# NORA MCP — sanitized runtime configuration template (thin split).
# Copy to `.env` and replace the placeholders before booting NORA.
# NEVER commit `.env`; it is gitignored.

# --- Driver layer (PMP 450i SNMP driver) -----------------------------------
# Directory holding per-vendor/per-firmware OID catalog JSON files.
# Layout: <path>/<vendor>/<model>/<firmware>.json.
NORA_OID_CATALOGS_PATH=./data/oid-catalogs/
# YAML inventory file loaded on boot. Public example lives at data/devices.example.yaml.
NORA_DEVICES_INVENTORY_PATH=./data/devices.yaml
# HMAC-SHA256 key for catalog file verification. Empty / unset → boot fails closed.
NORA_OID_CATALOG_SIGNING_KEY=change-me
# Optional override directory for system prompts. When unset, the registry reads the
# packaged prompts under src/nora/prompts/.
NORA_PROMPTS_DIR=

# --- Intervention memory MCP (Phase 3) --------------------------------------
# On-disk directory of intervention JSON records written by openchat's
# intervention_memory_tool. NORA never creates this directory — openchat owns it.
NORA_INTERVENTIONS_DIR=./var/interventions/
# Cap on keyword-search I/O (R7). WARNING logged when the cap fires.
NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS=1000
# Cap on correlate-scan I/O (R6).
NORA_INTERVENTIONS_CORRELATE_SCAN_LIMIT=50
```

The `loaded_from` detection logic in `config.py:104-131` and the
`settings_customise_sources` swap (lines 174-193) are both **KEPT** —
they still serve `.env` / process-env precedence for the surviving fields.

### Entry point inventory

| Surface today | Source | What the split must do |
|---|---|---|
| `python -m nora` | `src/nora/__main__.py` (`main`, line 40) + `[project.scripts] nora = "nora.__main__:main"` (`pyproject.toml:34`) | Becomes the **deprecation alias** (locked decision 1). Emits `warnings.warn(..., DeprecationWarning)` then delegates to the new `nora-mcp` entry point. |
| `nora` console script | Same as above | Same: re-targeted to the alias. The actual boot logic moves to the new script. |
| New `nora-mcp` console script | TBD: either `src/nora_mcp/__main__.py` (a new top-level package) or a thin wrapper `src/nora/cli.py` invoked from a new `[project.scripts]` entry | The canonical entry point. Wires `Settings → PromptRegistry → OidCatalogRegistry.verify_all → Inventory → set_driver → mcp.run`. |

Two concrete shapes work:

1. **New top-level package** `src/nora_mcp/__main__.py` (mirrors `src/nora/__main__.py` minus LLM/journal). Requires `[tool.hatch.build.targets.wheel] packages = ["src/nora", "src/nora_mcp"]`.
2. **Sub-module** `src/nora/cli.py` (called by a new console script `nora-mcp = "nora.cli:main"`). Leaves the wheel packaging alone; `__main__.py` becomes a deprecation re-export of `cli.main`.

Shape 2 is the more surgical option (no `pyproject.toml` wheel target change). The deprecation alias becomes:

```python
# src/nora/__main__.py — DEPRECATION ALIAS
from __future__ import annotations
import warnings

warnings.warn(
    "`python -m nora` is deprecated and will be removed in the next minor release. "
    "Use the `nora-mcp` console script instead.",
    DeprecationWarning,
    stacklevel=2,
)

from nora.cli import main  # noqa: E402
if __name__ == "__main__":
    main()
```

### Dependency analysis

**Drop from `[project.dependencies]` (lines 24-31):**

| Package | Used in | Reason |
|---|---|---|
| `lmstudio>=1,<2` | `src/nora/llm.py:20` (`import lmstudio as lms`) | `llm.py` eliminated. |
| `google-genai>=1,<3` | `src/nora/llm.py:21-22` (`from google import genai`, `from google.genai import errors`) | `llm.py` eliminated. |

**Keep:**

| Package | Reason |
|---|---|
| `fastmcp>=3.2,<4` | Server runtime. |
| `pydantic-settings>=2` | `Settings` base class. |
| `puresnmp==2.0.1` | Driver wire layer. |
| `puresnmp-crypto>=1.0` | Required for `puresnmp.Auth`/`Priv`. |

Dev-group is untouched (no dev deps are LLM- or journal-specific; the existing `snmpsim` + `pysmi` are driver-only).

### Test surface mapping

Tests that **MUST be deleted** (test concerns being eliminated):

| Path | What it tests | Verdict |
|---|---|---|
| `tests/test_llm.py` | `LMStudioProvider`, `GeminiProvider`, `build_provider`, factory cache | **DELETE entire file.** |
| `tests/test_server_auto_trace.py` | `_AutoTraceMiddleware`, end-to-end via FastMCP `Client` | **DELETE entire file.** |
| `tests/test_server_session_tools.py` | 4 `nora_session_*` tools over the FastMCP dispatcher | **DELETE entire file.** |
| `tests/test_session_journal_airgap.py` | AST scan over `nora.core/` for banned imports | **DELETE entire file.** |
| `tests/core/test_session_journal.py` | Public API: `record_step`, `get_state`, `set_focus`, `resume`, `summarize`, `_load_or_create_or_recover` | **DELETE entire file.** |
| `tests/core/test_session_journal_property.py` | Hypothesis property tests on `record_step` | **DELETE entire file.** |
| `tests/core/test_session_models.py` | `SessionState` / `SessionStep` validation | **DELETE entire file.** |
| `tests/core/test_session_paths.py` | Atomic write + 0o600 mode + symlink-escape guard | **DELETE entire file.** |
| `tests/core/test_session_redaction.py` | `REDACTION_LIST` + recursive `redact()` | **DELETE entire file.** |
| `tests/core/test_session_rotation.py` | NDJSON rotation | **DELETE entire file.** |
| `tests/core/test_session_summarize.py` | `summarize()` Markdown rendering | **DELETE entire file.** |
| `tests/core/__init__.py` | Marker only | **DELETE file.** |
| `tests/core/conftest.py` | `journal_dir` + `fresh_sanitizer` fixtures | **DELETE file.** |

Tests that **MUST be migrated** (file stays; some assertions / wiring change):

| Path | Migration |
|---|---|
| `tests/conftest.py` | Remove the `from nora import llm as llm_mod` import + the `_reset_llm_factory_cache` autouse fixture (lines 23-32). Remove the journal-related Settings kwargs from `hermetic_settings` (lines 154-157). |
| `tests/test_config.py` | Remove all tests that exercise `nora_llm_provider`, `lmstudio_*`, `gemini_*`. The `test_no_os_environ_in_src_nora` rule needs its allow-list re-evaluated: the new `cli.py` may also need to set process env for FastMCP banner suppression. `test_env_example_lists_every_read_variable` stays valid and actually gets stricter (fewer fields). |
| `tests/test_server.py` | Remove every `_call_nora_health` test (lines 77-327). Replace `test_main_module_boots_fastmcp_over_stdio` (line 289) — the source check for `"build_provider"` must change to a check for whatever the new boot wiring does (e.g., `"OidCatalogRegistry.verify_all"` + `"mcp.run"`). Update `test_mcp_instance_exposes_all_nine_tools` (line 479) — the tool set drops to 4 (`snmp_get_pmp450i_radio_metrics` + the 3 intervention tools). |
| `tests/test_integration.py` | Update the subprocess boot fixture: drop the env-var clearing for `NORA_LLM_PROVIDER`, `LMSTUDIO_MODEL_ID`, `GEMINI_API_KEY` (lines 97-98) and the comment about `nora_health` (lines 100-102). Update `test_subprocess_responds_to_tools_list_with_nora_health` (line 202) — the subprocess now lists the 4 surviving tools, and the test should be renamed and re-asserted. |
| `tests/test_server_driver_tool.py` | Remove the `nora_session_journal_*` Settings kwargs (lines 40-43). Remove `server_mod.register_auto_trace_middleware()` calls (lines 82, 87). The `nora_session_set_focus` mock setup (lines 68-70, 78) needs reworking once the driver loses its journal call (see "Migration seams"). |
| `tests/test_driver_snmpsim_v3.py`, `tests/test_driver_snmpsim_v2c.py`, `tests/test_driver_airgap.py`, `tests/test_driver_snmp_pmp450i.py` | Each monkeypatches `nora.drivers.snmp_pmp450i.driver.nora_session_set_focus` (lines 234, 183, 209, 300 respectively). Once the driver loses that call, these patches are no-ops and should be removed; the underlying tests otherwise stay green. |
| `tests/test_toolchain.py`, `tests/test_smoke.py`, `tests/test_prompts.py`, `tests/test_oid_catalog.py`, `tests/test_inventory.py`, `tests/test_driver_exceptions.py`, `tests/test_sanitizer.py` | **No change.** |
| `tests/intervention_memory/*` (7 files) | **No change.** Already independent of LLM / journal. |
| `tests/test_server_driver_tool.py` (the `set_focus` monkey-patch part) | See migration row above. |

### Migration seams (the minimum surgical changes)

1. **`src/nora/__main__.py` → deprecation alias** (full rewrite to the snippet in "Entry point inventory" above). ~12 lines.

2. **New `src/nora/cli.py`** with the canonical `main()`:
   - `os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")` (currently in `__main__.py:20`)
   - `Settings()` (no `provider`, no `init_session_journal`)
   - `PromptRegistry.from_settings(settings)` (boots prompts)
   - `OidCatalogRegistry.verify_all(settings)` (boots catalogs)
   - `Inventory.from_yaml(settings.nora_devices_inventory_path)`
   - `set_driver(Pmp450iDriver(inventory=..., catalog_registry=...))`
   - `mcp.run(show_banner=False)`
   - Drop the logger.info line about `active_provider`, `env_loaded`, `journal_dir` (those concepts go).

3. **`src/nora/server.py` deltas:**
   - Remove `_current_provider` / `LLMProvider` from module state and from `set_runtime_state` / `get_runtime_state`. Drop the `provider` parameter from the latter.
   - Remove `nora_health`, `nora_health_impl`, `_HEALTH_PROBE_SYSTEM`.
   - Remove `nora_session_get_state`, `nora_session_set_focus`, `nora_session_resume`, `nora_session_summarize` and the `_state_to_payload` helper.
   - Remove `init_session_journal` re-export + the `from nora.core.session_journal import (...)` block.
   - Remove `class _AutoTraceMiddleware`, `_derive_summary`, `register_auto_trace_middleware`, `unregister_auto_trace_middleware`, the `_auto_trace_registered` flag.
   - Trim `__all__` accordingly.
   - **No change** to the 4 surviving `@mcp.tool` blocks (`snmp_get_pmp450i_radio_metrics` + 3 intervention tools) — they already call `get_runtime_state()` which is refactored to return `(Settings,)` only.
   - **No change** to `_sanitizer = Sanitizer()` or to `configure_logging`.

4. **`src/nora/config.py` deltas:**
   - Remove LLM block (lines 52-56), `_DEFAULT_OPERATOR_ALIAS` (line 35), `ProviderName` (line 27).
   - Remove SessionJournal block (lines 58-71).
   - Drop the `_check_provider_credential` validator (lines 167-172).
   - Keep `_detect_loaded_from`, `__init__`, `settings_customise_sources` — all 7 surviving fields still need `.env` precedence.

5. **`src/nora/drivers/snmp_pmp450i/driver.py` seam (line 37-58 + line 115):**
   - Delete `_default_set_focus` and the module-level `nora_session_set_focus` alias.
   - Delete the `nora_session_set_focus(device_id)` call inside `fetch_radio_metrics` (line 115).
   - Drop the `nora_session_set_focus` re-export from `__all__` and from `drivers/snmp_pmp450i/__init__.py:34,59`.
   - **Unknown / open question (see Risks)**: what replaces the focus side-effect? Per locked decision 3 the SessionJournal is eliminated, so the driver can no longer record "I was queried for device X". Three options:
     - **(a)** Drop the side-effect entirely. Simplest; matches the "no LLM, no journal" cut.
     - **(b)** Replace with a structured stderr log line `"driver query: device_id={X}"`. Closest to a paper trail.
     - **(c)** Add a no-op `nora_session_set_focus` stub in `server.py` that just logs. Preserves the import surface but adds dead code.
   - Recommend **(a)** for the surgical cut; **(b)** if observability parity matters. This is a `sdd-propose` decision.

6. **`pyproject.toml` deltas (lines 24-46):**
   - `dependencies`: drop `lmstudio>=1,<2` + `google-genai>=1,<3`.
   - `[project.scripts]`: change `nora = "nora.__main__:main"` → `nora = "nora.__main__:main"` (same target — it's now the alias body). Add `nora-mcp = "nora.cli:main"`.

7. **`.env.example`**: replace with the 7-key version above.

## Approaches

1. **Sub-module `cli.py` + deprecation `__main__.py`** *(Recommended)*
   - **Pros**: No wheel packaging change. Minimal touch surface. `nora-mcp` is one console-script line. The deprecation alias is 12 lines. The split is a single coherent PR.
   - **Cons**: `nora.cli` and `nora.__main__` both exist in the same package (some may prefer a separate top-level `nora_mcp` package for clearer identity).
   - **Effort**: Low.

2. **New top-level `src/nora_mcp/` package**
   - **Pros**: Clear identity; future `nora-service` (Phase 4) can live alongside without confusion.
   - **Cons**: Requires `[tool.hatch.build.targets.wheel] packages = ["src/nora", "src/nora_mcp"]`. Two `__init__.py` with two `__version__`. More packaging surgery.
   - **Effort**: Medium.

3. **Move boot into `server.py`** (drop `__main__.py` entirely, replace with a 3-line deprecation stub)
   - **Pros**: Even thinner; the canonical entry point is `nora-mcp = "nora.server:run"` (after extracting a `run()` function from the module).
   - **Cons**: Couples the boot sequence to the server module; loses the clean `cli` namespace; re-exported tools in `server.__all__` become harder to audit.
   - **Effort**: Low–Medium.

## Recommendation

**Approach 1** (`src/nora/cli.py` + deprecation `__main__.py`). It is the most surgical: no `pyproject.toml` wheel target change, no new package, no module re-shuffling — just one new 40-line `cli.py`, a 12-line `__main__.py` alias, and the server/config/driver trims above. Locked decision 4 (big bang deploy, single PR) is naturally satisfied because every change is mechanical and self-contained.

For the open driver question, recommend dropping the focus side-effect entirely (Approach 5a). If the user later wants an audit trail, Approach 5b (structured stderr log) is a follow-up one-liner.

## Risks

1. **Hidden external caller of `nora_health`**: A grep for `nora_health` over the repo finds it in `src/nora/server.py`, `tests/`, and `openspec/specs/nora-mcp-server/spec.md` only — **no external docs or out-of-repo integration points**. Risk: low. Unknown: whether openchat or another MCP client currently consumes `nora_health` for status; if so, they will break silently.
2. **Hidden external caller of the four `nora_session_*` tools**: Same grep result — only repo-internal. Risk: low.
3. **`nora_session_set_focus` in the driver**: removing it removes the only side-effect of `Pmp450iDriver.fetch_radio_metrics`. If any test or operator workflow depends on the journal recording "queried device X" before returning metrics, that workflow breaks silently. (Verified: no test asserts on the focus being set BEFORE the metric fetch — they only mock the symbol.)
4. **Big-bang deploy (locked decision 4)**: the `lmstudio` + `google-genai` deps drop in the same PR. Any operator who has those packages installed will see `pip` / `uv` prune them on the next `uv sync`. No code path imports them post-cut, so this is a clean removal.
5. **`hermetic_settings` in `tests/conftest.py:151-162` is also imported by `tests/test_server_driver_tool.py` and `tests/test_server_auto_trace.py`**: the former needs migration (drop journal kwargs); the latter is being deleted entirely. Both are reachable through `tests/conftest.py`, so the fixture's signature changes propagate.
6. **`Settings.pydantic-settings` precedence swap** (`settings_customise_sources`, lines 174-193): KEEPS its `dotenv > env > defaults` behaviour. The `secure-configuration/spec.md` R1 contract still holds for the surviving fields. Risk: none if `.env.example` is regenerated.
7. **Openchat `webui.db` mirror shim** (`src/nora/intervention_memory/shim_webui.py:44`) constructs `Settings(_env_file=None, _env_file_encoding=None)` — this still works post-cut (the surviving 7 fields accept those kwargs). No migration.
8. **The `prompt-registry` spec** (`openspec/specs/prompt-registry/spec.md`) declares a contract that the registry loads packaged prompts by default. This survives the cut verbatim because the prompt registry is unrelated to LLM.
9. **`test_env_example_lists_every_read_variable`** (`tests/test_config.py:96`): currently asserts every `Settings.model_fields` key has an `.env.example` line. Post-cut, this test PASSES with the 7-key `.env.example` because the 9 removed fields no longer exist in `Settings`. No test change needed (other than the ones above).
10. **MyPy `--strict` over `src/nora/`** (`pyproject.toml:68-71`): after the cut, `server.py` no longer references `LLMProvider`, `__main__.py` no longer references `build_provider` / `init_session_journal`, and `config.py` no longer references `ProviderName` / `_DEFAULT_OPERATOR_ALIAS`. The strict build stays green.

### Open question (UNRESOLVED — must be resolved in `sdd-propose`)

What replaces `Pmp450iDriver.fetch_radio_metrics`'s side-effect call to the journal? See Migration seam #5. The recommendation is "drop it"; the user should explicitly confirm before `sdd-propose`.

## Ready for Proposal

**Yes**, with one decision needed up-front:

> **Q for orchestrator**: Confirm whether the `Pmp450iDriver.fetch_radio_metrics` call to the SessionJournal (`drivers/snmp_pmp450i/driver.py:115`) should be (a) dropped entirely, (b) replaced with a structured stderr log line, or (c) kept as a no-op stub. The recommended default is **(a)** — the simplest surgical cut. This is the only seam that is not fully determined by the locked decisions.