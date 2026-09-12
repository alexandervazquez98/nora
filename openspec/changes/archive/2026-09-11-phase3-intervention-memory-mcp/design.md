# Design: NetOps Persistent Memory & Correlation MCP — NORA Slice

## 1. Architecture Overview

```
                    ┌──────────────────────────────────────────────┐
                    │  LLM agent (LM Studio / Open WebUI / MCP    │
                    │  client)                                    │
                    └──────────────────┬───────────────────────────┘
                                       │ tool call (JSON-RPC or in-proc)
                  ┌────────────────────┴───────────────────────┐
                  │                                            │
                  ▼                                            ▼
   ┌────────────────────────────┐         ┌─────────────────────────────┐
   │  src/nora/server.py        │         │  shim_webui.py (Tools)      │
   │  @mcp.tool registrations   │         │  class Tools: async def …   │
   │  (3 wrappers, 3-line body) │         │  consumed by openchat via   │
   │  ↳ nora.session_journal    │         │  inspect.getsource(Tools)   │
   │    records every call      │         │                             │
   └──────────┬─────────────────┘         └──────────┬──────────────────┘
              │ deleg. (sync)                       │ deleg. (async)
              └─────────────────────┬───────────────┘
                                    ▼
                       ┌────────────────────────┐
                       │  tools.py (pure sync)  │ ← source of truth
                       │  search_history()      │
                       │  get_lifecycle_summary │
                       │  correlate_sector_int. │
                       └──┬────────┬────────┬───┘
                          │        │        │
                          ▼        ▼        ▼
               ┌──────────┐ ┌────────┐ ┌──────────────┐
               │storage.py│ │sanitize│ │correlation.py│
               │glob+skip │ │  .py   │ │ pure fns     │
               │on error  │ │ bypass │ │              │
               └────┬─────┘ └────┬───┘ └──────────────┘
                    │            │
                    ▼            ▼
            ┌──────────────┐ ┌──────────────────┐
            │ /var/interv/ │ │ Sanitizer        │
            │ *.json files  │ │ (existing)      │
            └──────────────┘ └──────────────────┘
```

One-way dependency arrows. `shim_webui.py` and `server.py` both delegate to the same `tools.py` functions; the next openchat deploy renders `inspect.getsource(Tools)` into `webui.db` so the two surfaces cannot drift.

## 2. Module Breakdown

### Production (`src/nora/intervention_memory/`)

| File | Responsibility | Public API | Key notes | Imports | Deps |
|---|---|---|---|---|---|
| `__init__.py` | Public surface | Re-export `InterventionMemoryRecord`, `NetworkEquipmentBlock`, `PreExistingOfflineSubscriber`, `search_history`, `get_lifecycle_summary`, `correlate_sector_interference`, `Tools` (via `shim_webui`), `sanitize_record_payload`. Add module-docstring repeating the read-only hard rule. | ~25 LOC | submodules | `tests`, openchat deploy |
| `models.py` | Pydantic models with `extra="ignore"`; `Literal` enums for `stage` / `status`; every non-required field `Optional[T] = None`. | `InterventionMemoryRecord`, `NetworkEquipmentBlock`, `PreExistingOfflineSubscriber` | Canonical `mac: Optional[str] = None`; `mac_address` alias survives via `extra="ignore"`. | `pydantic` | `tools.py`, `sanitize.py`, `storage.py` |
| `storage.py` | Read-only glob + tolerant parsing. | `read_records(settings) -> list[InterventionMemoryRecord]` | Never raises on missing dir (returns `[]`). Per-file `try/except (json.JSONDecodeError, ValidationError)` → `logger.warning(filename); continue`. Mirrors `_load_or_create_or_recover` from `core/session_journal.py:452`. | `models`, `json`, `glob`, `pathlib` | `tools.py` |
| `sanitize.py` | Recursive walker; bypass list. | `sanitize_record_payload(record, sanitizer) -> dict[str, Any]` | `_BYPASS_FIELDS = frozenset({"target_ip","intervention_id","stage","status","timestamp_unix","timestamp_iso","created_at","ticket_number"})` checked at every dict key before sanitizing. | `nora.sanitizer.Sanitizer` | `tools.py` |
| `correlation.py` | Pure tower + frequency match. | `match_tower(system_name, tower_name) -> bool`, `classify_conflict(carrier, target, width) -> Literal["CO_CHANNEL","ADJACENT_CHANNEL"]` | `tower_name.lower() in system_name.lower()`; `delta_mhz < 0.5` → CO; `< width` → ADJACENT. `CLEAR` returned only when caller finds no match. | stdlib | `tools.py` |
| `tools.py` | Source-of-truth: the three tool bodies. | `search_intervention_history(...)`, `get_lifecycle_summary(...)`, `correlate_sector_interference(...)` (all sync, all take `Settings` as first arg). | All three call `storage.read_records`, then `sanitize_record_payload`, then sort + slice. Return `dict` (server.py serializes via FastMCP). | `storage`, `sanitize`, `correlation`, `models` | `server.py`, `shim_webui.py` |
| `shim_webui.py` | `class Tools` with 3 `async def` delegators. | `Tools` (no methods exposed publicly besides the three) | Each method accepts and ignores `__event_emitter__`. Signatures MUST mirror `tools.py` for `inspect.getsource(Tools)` parity. | `tools` | openchat deploy (out of repo) |

Estimated production LOC: **~530**.

### Tests (`tests/intervention_memory/`)

| File | Covers | Key fixtures |
|---|---|---|
| `test_models.py` | R1 — v7/v8 parse, alias tolerance, invalid literal rejection | `fixtures/v7_baseline.json`, `fixtures/v8_pre_migration.json` |
| `test_storage.py` | R3 — corrupt-skip, ValidationError-skip, missing-dir-returns-`[]` | `}.invalid.json`, `validation_failure.json` |
| `test_correlation.py` | R6 — `match_tower` + `classify_conflict` truth table | none |
| `test_sanitize.py` | R4 free-text sanitized; R6 neighbor sanitize; R9 IPv4/MAC mask + structured bypass | record fixture |
| `test_tools.py` | R4/R5/R6/R7 + R10 shim delegation; shim presence check | `tmp_path` of 3 fixture records |
| `test_no_writes.py` | R2 AST scan + R11 no-pytest-imports | uses `tests/intervention_memory/fixtures/` (read-only) |

### Wiring touches

| File | Change | LOC |
|---|---|---|
| `src/nora/server.py` | Add `from nora.intervention_memory.tools import (search_intervention_history, get_device_lifecycle_summary, correlate_sector_interference)`. Add 3 `@mcp.tool` wrappers (3-line bodies that call `get_runtime_state()` then delegate). Re-export the 3 names in `__all__`. | +28 |
| `src/nora/config.py` | Add 3 fields (next to `nora_session_journal_*` block): `nora_interventions_dir: Path = Path("./var/interventions/")`, `nora_interventions_keyword_search_max_records: int = 1000`, `nora_interventions_correlate_scan_limit: int = 50`. The existing `extra="ignore"` already shields callers. | +9 |
| `.env.example` | Append new section "Intervention memory MCP" with the 3 keys; the directory key defaults to `./var/interventions/`; the cap keys are commented-out example overrides. No real production path. | +12 |

## 3. Data Flow Examples

### Scenario A — MCP caller

```
LLM (in Open WebUI / LM Studio)
  └─ JSON-RPC → server.search_intervention_history(target_ip="10.53.12.4", limit=5)
       └─ @mcp.tool wrapper: settings, _ = get_runtime_state()
            └─ tools.search_intervention_history(settings, target_ip="10.53.12.4", limit=5)
                 ├─ storage.read_records(settings)         # glob *.json, parse, skip on error
                 ├─ filter: target_ip exact match
                 ├─ sort: timestamp_unix DESC
                 ├─ slice: [:5]
                 ├─ sanitize_record_payload(r, sanitizer)   # bypasses target_ip; sanitizes findings_and_dictamen
                 └─ return list[dict]
  ← FastMCP serialises list[dict] to JSON-RPC response
  ← _AutoTraceMiddleware records 1 SessionStep {tool, input_args, result_summary, outcome: success}
```

### Scenario B — webui.db shim caller (Open WebUI in-process)

```
LLM (Open WebUI) → webui.db tool entry → openwebui spawns Tools().<method>(**kwargs) in-process
  └─ await Tools.search_intervention_history(target_ip="10.53.12.4", limit=5, __event_emitter__=...)
       └─ tools.search_intervention_history(settings=..., ...)   # same source of truth
            ... (identical body to Scenario A from here) ...
  ← returns JSON string (webui.db convention)
```

### Scenario C — AST test catches a future regression

```
Developer adds `Path("/tmp/x").write_text("x")` to storage.py
  └─ git commit
       └─ CI runs `uv run pytest tests/intervention_memory/test_no_writes.py`
            └─ test_writes_via_path_write_text_fail_build:
                 ast.walk(storage.py) → ast.Call attribute="write_text"
                 offenders = [("storage.py", 42, "write_text")]
                 assert offenders == []  # FAILS
                 message: "Write call detected: (src/nora/intervention_memory/storage.py, 42, 'write_text')"
```

## 4. Sanitizer Contract (target_ip Bypass)

`_BYPASS_FIELDS = frozenset({"target_ip", "intervention_id", "stage", "status", "timestamp_unix", "timestamp_iso", "created_at", "ticket_number"})`.

Recursive walker in `sanitize.py` (`_sanitize_dict(value, sanitizer)`):

| Field | Action | Rationale |
|---|---|---|
| `target_ip` | **BYPASS** (verbatim) | User's explicit decision in preflight; intervention records ARE operator data; LLM uses IP as direct reference. |
| `intervention_id`, `ticket_number`, `stage`, `status`, `timestamp_unix`, `timestamp_iso`, `created_at` | BYPASS | Typed scalars / enum strings / UTC ISO / int — not free text. |
| `record_name`, `findings_and_dictamen`, `recommended_action`, `agent_name` | Sanitize via `Sanitizer.sanitize(...)` | Free text authored by humans / LLMs. |
| `network_equipment.system_name`, `network_equipment.hardware_band` | Sanitize (hostname / band text) | Free text. |
| `network_equipment.pre_existing_offline_subscribers[*].note` | Sanitize | Free text. |
| `network_equipment.pre_existing_offline_subscribers[*].mac`, `.ip` | Sanitize (MAC, IPv4 in RFC1918) | Even though structurally typed, may carry secrets. |
| `network_equipment.pre_existing_offline_subscribers[*].luid`, `.uptime` | Pass through unchanged | LUID is internal index; uptime is operator-facing string. |

`_sanitize_dict` recursion: dict → check `key in _BYPASS_FIELDS` first; if yes, copy value verbatim; else recurse on value. list → recurse on every item. str → `sanitizer.sanitize(s).text`. Other types → copy.

The MCP return shape preserves the bypass even when nested inside `latest_intervention` (R-NEW-2 — top-level bypass list inherited as-is from `session-journal` R6).

## 5. Storage Layer

| Concern | Decision | Why |
|---|---|---|
| Missing dir | Return `[]`; do NOT create | NORA does not own the dir (openchat writer does). |
| Per-file error | `try: json.load + InterventionMemoryRecord.model_validate except (json.JSONDecodeError, ValidationError): logger.warning(name); continue` | Mirror `core/session_journal.py:452-469`. One bad file cannot take down the tool. |
| Filename | Glob `*.json`; NORA does NOT inspect filename | openchat owns the `{ticket}_{ip}_{stage}_{timestamp}.json` shape; we only care about JSON content. |
| Sort + slice | `sorted(records, key=lambda r: r.timestamp_unix, reverse=True)[:limit]` | Per R4-S3; same as prototype. |
| Cap | Read at most `Settings.nora_interventions_keyword_search_max_records` files when `keyword` filter is provided (R7) | Bound I/O on a large dir; WARNING logged when the cap fires. |
| Locking | None | NORA is read-only; openchat handles writer concurrency. |

## 6. Correlation Layer

Pure functions, no I/O:

```python
def match_tower(system_name: str, tower_name: str) -> bool:
    return tower_name.lower() in system_name.lower()

def classify_conflict(carrier: float, target: float, width: float) -> Literal["CO_CHANNEL","ADJACENT_CHANNEL"]:
    delta = abs(carrier - target)
    if delta < 0.5:
        return "CO_CHANNEL"
    if delta < width:
        return "ADJACENT_CHANNEL"
    return "CLEAR"
```

Documented caveat (in module docstring + `correlate_sector_interference` docstring): substring match false-positives on short prefixes (e.g., `tower_name="A"` matches `*-A`). A structured `tower` field is the future fix (out of scope for this slice).

## 7. Server Wiring (the 3 `@mcp.tool`)

Each is a 3-line body in `src/nora/server.py`:

| Tool | Args | Return | Body |
|---|---|---|---|
| `search_intervention_history` | `target_ip=None, ticket_number=None, stage=None, keyword=None, limit=5` | `list[dict[str, Any]]` | `settings, _ = get_runtime_state(); return _search(settings=settings, target_ip=..., ticket_number=..., stage=..., keyword=..., limit=...)` |
| `get_device_lifecycle_summary` | `target_ip: str` | `dict[str, Any]` | `...; return _summary(settings=settings, target_ip=...)` |
| `correlate_sector_interference` | `tower_name: str, target_frequency_mhz: float, channel_width_mhz: float = 20.0` | `dict[str, Any]` | `...; return _correlate(settings=settings, tower_name=..., target_frequency_mhz=..., channel_width_mhz=...)` |

Each carries an LLM-facing docstring that quotes the spec definition + mentions sanitization + mentions the read-only guarantee.

`_AutoTraceMiddleware` records every call (R-NEW-1 ADDED Scenario 3); no middleware change.

## 8. Shim for webui.db

`class Tools` in `shim_webui.py` — three `async def` methods + constant fields the openchat deploy script overwrites (`INTERVENTIONS_DIR`, `KEYWORD_SEARCH_MAX_RECORDS`, `CORRELATE_SCAN_LIMIT`). Each method delegates 1:1 to `tools.py`:

```python
class Tools:
    INTERVENTIONS_DIR = ""
    KEYWORD_SEARCH_MAX_RECORDS = 1000
    CORRELATE_SCAN_LIMIT = 50

    async def search_intervention_history(
        self, target_ip=None, ticket_number=None, stage=None,
        keyword=None, limit=5, __event_emitter__=None, **_unused,
    ) -> str:
        settings = _settings_from_env_or_singleton()
        return json.dumps(search_history(
            settings=settings, target_ip=target_ip, ticket_number=ticket_number,
            stage=stage, keyword=keyword, limit=limit,
        ))

    # ... get_device_lifecycle_summary, correlate_sector_interference same shape
```

Dependency direction is one-way: `shim_webui.py` imports from `nora.intervention_memory.tools`; no production module imports from `shim_webui` (R-NEW-4 ADDED Scenario 3). The openchat deploy script consumes `inspect.getsource(Tools)` — drift is structurally impossible because both surfaces render the same `tools.py` body.

## 9. AST Test Design (`test_no_writes.py`)

Mirror `tests/test_driver_airgap.py` — `ast` walker over every `.py` under `src/nora/intervention_memory/`. Two checks:

**Check 1 — write-call scan (R2).** Two passes:

1. `ast.Call` where `func` is one of: `open` (mode arg ∈ `{"w","a","x","+"}`); `Path.write_text` / `Path.write_bytes` / `Path.unlink`; `os.replace` / `os.remove` / `os.removedirs` / `os.makedirs`; `shutil.rmtree`. For `open`, ignore when mode is `"r"`, `"rb"`, or the call has no mode arg (read by default).
2. Regex fallback for `ast`-missed constructs: `\.write_text\(`, `\.write_bytes\(`, `\.unlink\(`, `os\.replace\(`, `os\.remove\(`, `shutil\.rmtree\(`.

Offenders are tuples `(relative_path, lineno, call_name)`; any nonzero offenders fails the build with a path:line:call message.

**Check 2 — banned-import scan (R11).** Grep every `.py` for `import pytest`, `from pytest`, `import _pytest`, `from _pytest`. Zero matches in production code.

## 10. Test Matrix (RED → GREEN)

| Req | Scenario | Test file | Test function | RED | GREEN |
|---|---|---|---|---|---|
| R1 | v7 no `recommended_action` | `test_models.py` | `test_v7_record_parses_without_recommended_action` | `model_validate` raises | returns `None` for the field |
| R1 | v8 `mac_address` tolerated | `test_models.py` | `test_v8_record_with_mac_address_alias_is_tolerated` | raises on the alias | `record.mac is None`, no raise |
| R1 | invalid stage literal | `test_models.py` | `test_invalid_stage_literal_raises_validation_error` | accepts bogus | `ValidationError` |
| R2 | AST scan finds zero writes | `test_no_writes.py` | `test_no_writable_file_calls_under_intervention_memory` | offenders non-empty | offenders `== []` |
| R2 | injected `Path.write_text` breaks build | `test_no_writes.py` | `test_injected_write_text_call_fails_build` (uses a tmp copy + monkeypatch) | no offender detected | offender `("storage.py",N,"write_text")` |
| R2 | `open(..., "r")` allowed | `test_no_writes.py` | `test_open_read_mode_is_allowed` | flagged | not flagged |
| R3 | corrupt JSON skipped | `test_storage.py` | `test_corrupt_json_file_is_skipped` | raises | 3 records + WARNING |
| R3 | ValidationError skipped | `test_storage.py` | `test_validation_error_is_skipped` | raises | 1 record + WARNING |
| R3 | missing dir returns `[]` | `test_storage.py` | `test_missing_directory_returns_empty_list` | returns nothing | returns `[]`, no `mkdir` |
| R4 | `target_ip` exact match | `test_tools.py` | `test_search_target_ip_exact_match` | ignores IP filter | 2 records |
| R4 | keyword substring | `test_tools.py` | `test_search_keyword_substring_match` | filters text | returns the record |
| R4 | sort DESC | `test_tools.py` | `test_results_sorted_by_timestamp_desc` | returns in load order | `[300,200,100]` |
| R4 | limit clamps | `test_tools.py` | `test_limit_clamps_result_set` | returns all 5 | returns 2 |
| R4 | free-text sanitized | `test_sanitize.py` | `test_free_text_fields_sanitized_in_search_output` | contains `10.53.12.4` | contains `RADIO_NODE_*` |
| R5 | empty → `NO_HISTORY_FOUND` | `test_tools.py` | `test_lifecycle_summary_no_history_returns_status_marker` | empty dict | exact dict match |
| R5 | SUCCESS with all 6 fields | `test_tools.py` | `test_lifecycle_summary_success_returns_all_six_fields` | missing `stages_recorded` | full 6-field dict |
| R5 | offline subscribers from latest PRE_DIAGNOSTIC | `test_tools.py` | `test_known_pre_existing_offline_subscribers_from_latest_pre_diagnostic` | picks earliest | picks latest |
| R6 | equal carrier CO_CHANNEL | `test_correlation.py` | `test_correlate_equal_carrier_returns_co_channel` | ADJACENT | CO_CHANNEL, `delta == 0.0` |
| R6 | nearby carrier ADJACENT | `test_correlation.py` | `test_correlate_nearby_carrier_returns_adjacent` | CO_CHANNEL | ADJACENT |
| R6 | tower mismatch zero conflicts | `test_correlation.py` | `test_correlate_tower_mismatch_returns_zero_conflicts` | 1 conflict | `detected_conflicts == []`, `is_frequency_clear_on_tower == True` |
| R6 | correlate result sanitized | `test_sanitize.py` | `test_correlate_result_neighbor_sanitized` | contains private IP | contains `RADIO_NODE_*` |
| R7 | keyword cap + warning | `test_tools.py` | `test_keyword_search_cap_enforced_and_warning_logged` | reads 1500 | reads ≤ 1000 + WARNING |
| R7 | under cap no warning | `test_tools.py` | `test_keyword_search_under_cap_no_warning` | WARNING emitted | no WARNING |
| R8 | env var overrides | `test_config.py` (existing) | reuse existing pattern with new fields | default | override |
| R8 | defaults apply | `test_config.py` (existing) | reuse | override | defaults |
| R9 | IPv4 in `record_name` masked | `test_sanitize.py` | `test_ipv4_in_record_name_is_masked` | contains `10.0.0.5` | contains `RADIO_NODE_*` |
| R9 | MAC in `note` masked | `test_sanitize.py` | `test_mac_in_subscriber_note_is_masked` | contains MAC | contains `SWITCH_ACC_*` |
| R9 | structured top-level bypass | `test_sanitize.py` | `test_structured_top_level_fields_bypass_sanitizer` | mangled | byte-identical |
| R10 | shim exposes 3 async methods | `test_tools.py` | `test_shim_tools_class_exposes_three_async_methods` | intent missing | 3 methods present |
| R10 | shim delegates no drift | `test_tools.py` | `test_shim_methods_delegate_to_tools_module` (monkeypatch tools.search) | drift | patched fn called exactly once |
| R10 | production does not import shim | `test_no_writes.py` | `test_production_modules_do_not_import_shim_webui` | match | zero matches |
| R11 | coverage ≥ 85% | (verify) | n/a — `pytest --cov` gate | < 85% | ≥ 85% |
| R11 | production no pytest import | `test_no_writes.py` | `test_production_modules_do_not_import_pytest` | import present | zero matches |

34 scenarios → 34 RED tests; `test_no_writes.py` doubles as R2 AST + R10 grep + R11 grep.

## 11. Coverage & Linting

| Gate | Target | Source of truth |
|---|---|---|
| Line coverage (per-package) | ≥ 88% (estimate from proposal R11) | `openspec/config.yaml:116` (floor 85%) |
| New test count | ≥ 34 | this design §10 |
| `mypy --strict src/nora` | 0 errors | `pyproject.toml:69` |
| `ruff check .` | 0 errors | `pyproject.toml:62` |
| `ruff format --check .` | 0 errors | `pyproject.toml` |
| AST scan | 0 offenders | `test_no_writes.py` |
| `pytest --strict-markers --strict-config` | exits 0 | orchestrator preflight |

## 12. Forecast Update

Refined LOC by file (per §2):

| File | LOC (forecast) |
|---|---|
| `src/nora/intervention_memory/__init__.py` | 25 |
| `src/nora/intervention_memory/models.py` | 95 |
| `src/nora/intervention_memory/storage.py` | 110 |
| `src/nora/intervention_memory/sanitize.py` | 75 |
| `src/nora/intervention_memory/correlation.py` | 60 |
| `src/nora/intervention_memory/tools.py` | 130 |
| `src/nora/intervention_memory/shim_webui.py` | 70 |
| `tests/intervention_memory/test_models.py` | 110 |
| `tests/intervention_memory/test_storage.py` | 95 |
| `tests/intervention_memory/test_correlation.py` | 75 |
| `tests/intervention_memory/test_sanitize.py` | 90 |
| `tests/intervention_memory/test_tools.py` | 150 |
| `tests/intervention_memory/test_no_writes.py` | 80 |
| `tests/intervention_memory/fixtures/*` (3 files) | 90 |
| `src/nora/server.py` (+28) | 28 |
| `src/nora/config.py` (+9) | 9 |
| `.env.example` (+12) | 12 |
| **Total authored additions** | **~1304** |
| **Total modifications** | **49** |
| **Total diff (`git diff --stat`)** | **~1353** |

**400-line PR-review budget analysis (refined):**

| Slice | LOC | Notes |
|---|---|---|
| Single PR | **~1353** | **HIGH risk** (~3.4× budget) |
| Chained PR1 — pure logic + tests | **~1205** | new package + tests + fixtures. Review focus: schema tolerance + sanitization boundary + read-only guarantee + AST guard. |
| Chained PR2 — NORA wiring | **~49** | `server.py` + `config.py` + `.env.example`. Review focus: pure-function delegation, Settings threading, env hygiene. |

**Recommendation to apply gate:** **Chained PRs are clearer than `size:exception` single PR.** The pure-logic + tests slice is reviewable because the AST guard + per-file `< 150 LOC` tests make scope narrow. The wiring slice is tiny. Openchat's deploy of `inspect.getsource(Tools)` is out of NORA's diff. The apply gate's Review Workload Guard should ask the user once before opening PR1.

## Cross-References

- **Spec authority**: `openspec/changes/phase3-intervention-memory-mcp/specs/intervention-memory/spec.md` (R1–R11) + `specs/nora-mcp-server/spec.md` (R-NEW-1..4).
- **Reused patterns**: `_load_or_create_or_recover` from `src/nora/core/session_journal.py:452`; AST walker from `tests/test_driver_airgap.py`; sanitizer-tree walker from `src/nora/core/session_journal.py:_sanitize_tree`.
- **External consumers**: openchat's `deploy_v9_nora_intervention_memory.py` (out of repo) renders `inspect.getsource(Tools)` into `webui.db`.

## Open Questions

- None blocking. The `target_ip` bypass is locked in preflight. Operator wires the real `NORA_INTERVENTIONS_DIR` on `.22`; no production path enters the repo.
