# Proposal: phase3-intervention-memory-mcp — NetOps Persistent Memory & Correlation MCP

> SCOPE.md phase alignment: **Phase 4 (FastMCP server surface)** plus a read-only consumer role for Phase 5 (diagnostic reasoning). Read-only on disk; no device writes. Hits `openspec/config.yaml` rule `proposal.0` (rollback plan) + rule `proposal.2` (reject autonomous mutation — satisfied: all 3 tools are read).

## 1. Intent

NORA exposes three read-only FastMCP tools that consume the on-disk intervention JSON files openchat's existing webui.db-registered `intervention_memory_tool` already writes, so any LLM agent attached to NORA's `mcp` instance can search history, summarise a device lifecycle, and detect tower co/adjacent-channel conflicts without NORA ever owning writes.

## 2. Scope

### In Scope
- New package `src/nora/intervention_memory/` with pure logic (storage parsing, correlation, sanitization-on-read).
- Three `@mcp.tool` registrations on the global `mcp` instance in `src/nora/server.py` (`search_intervention_history`, `get_device_lifecycle_summary`, `correlate_sector_interference`).
- One `Tools`-class shim (`shim_webui.py`) callable from openchat's deploy script to produce a copy-paste block for `webui.db`'s `tool` table — same source of truth as the MCP wrappers.
- Pydantic models `InterventionMemoryRecord`, `NetworkEquipmentBlock`, `PreExistingOfflineSubscriber` with `extra="ignore"` and every non-required field `Optional[...] = None` — tolerates v7 + v8 prototype records.
- New `Settings` keys: `nora_interventions_dir`, `nora_interventions_keyword_search_max_records` (default `1000`), `nora_interventions_correlate_scan_limit` (default `50`).
- `.env.example` extension with `NORA_INTERVENTIONS_DIR=./var/interventions/` (relative default; operator wires real path on `.22`).
- AST-level read-only contract test under `tests/intervention_memory/test_no_writes.py` mirroring `tests/test_driver_airgap.py`.

### Out of Scope
- Any write tool, including HITL-gated `save_intervention_record` mirroring openchat's writer (openchat owns the writer per SCOPE.md §4.C zero-modification rule).
- Schema migration / on-disk rewrite of v7 → v8 records (Pydantic tolerates both shapes; openchat owns the writer side).
- A second FastMCP server alongside NORA (the 3 tools go on NORA's existing `mcp` instance per explore §"Approaches" decision).
- Wiring NORA's stdio server into Open WebUI — `webui.db` registration is openchat's deploy script's job; NORA ships the shim, openchat's `deploy_v9_nora_intervention_memory.py` (next deploy) pastes the rendered block.
- Multi-tenant isolation of the interventions dir (openchat's concern).
- Any new runtime dependency (stdlib `json`/`glob`/`pathlib`; `pydantic` is already a transitive of `pydantic-settings`).

### Non-Goals
Explicit restatement of SCOPE.md §5 non-goals + the 6 hard "no"s from explore.md §"Non-Goals": no writes, no HITL for memory writes, no schema migration, no multi-tenant, no NORA → `.22` network egress, no `pytest-asyncio` introduction. **NORA never reaches out to the openchat server; it reads local disk only.**

## 3. Approach

### Chosen — Source-of-truth + two thin wrappers (RECOMMENDED)
Pure logic lives in `src/nora/intervention_memory/{models,storage,correlation,sanitize}.py`. The piece the LLM-facing surface needs is exposed twice:

1. **MCP wrapper**: `@mcp.tool` functions in `src/nora/server.py`, each 3-line body that calls into the pure logic. Each call auto-traced by the existing `_AutoTraceMiddleware` (no middleware change).
2. **webui.db shim**: `src/nora/intervention_memory/shim_webui.py` exposes a `class Tools` whose async methods `async def search_intervention_history(...)`, `async def get_device_lifecycle_summary(...)`, `async def correlate_sector_interference(...)` import the pure logic. openchat's deploy script reads `inspect.getsource(ShimTools)` and writes the rendered text into the `webui.db` `tool.content` column (mirror of what `deploy_v7_intervention_memory.py` already does for `intervention_memory_tool`). Same code path → no drift.

### Alternative — MCP wrappers only, ask webui.db user to re-import
- Skip the shim. Pro: ~20 fewer LOC. Con: future operator changes the storage path / scan cap and Open WebUI's native tool silently keeps the old behaviour; the LLM calling `intervention_memory_tool` via webui.db sees a different result set than the same agent calling NORA via MCP. Drift is the failure mode the shim exists to prevent.

### Why source-of-truth wins
- The user picked "Espejo webui.db" — they want both surfaces. Two implementations = two truths = guaranteed drift on the very next config bump.
- `inspect.getsource(ShimTools)` is one line in openchat's deploy script. The shim is a 5-line file. Cost of de-drift is zero. Drift risk of no-shim is high. Asymmetry.
- The existing prototype (`deploy_v7_intervention_memory.py`) already follows this pattern: `Tools` class with `async def` methods, in-process execution by Open WebUI. The shim is just NORA's version of the same shape, with the bodies delegating to library code instead of duplicating it.

## 4. Architecture

### Modules (`src/nora/intervention_memory/`)

| File | Responsibility | Imports | LOC est. |
|---|---|---|---|
| `__init__.py` | Public re-exports: `InterventionMemoryRecord`, `load_records`, `search`, `summarise`, `correlate` | — | 15 |
| `models.py` | `InterventionMemoryRecord`, `NetworkEquipmentBlock`, `PreExistingOfflineSubscriber` (Pydantic BaseModel, `extra="ignore"`) | `pydantic` | 90 |
| `storage.py` | Pure read: glob `*.json` under `Settings.nora_interventions_dir`, parse via `models.py`, skip-on-error with `logger.warning`, return `list[InterventionMemoryRecord]` | `models`, stdlib `json/glob/pathlib` | 110 |
| `sanitize.py` | `_sanitize_record(record, Sanitizer)` walks every free-text string in a record through `Sanitizer.sanitize` (mirrors `session_journal._sanitize_tree`); structured top-level fields `intervention_id`/`timestamp_unix`/`stage`/`status`/`ticket_number`/`target_ip` bypass by config (typed scalars, not free text). | `nora.sanitizer.Sanitizer` | 60 |
| `correlation.py` | Pure functions: `match_tower(system_name, tower_name) -> bool` (substring `lower()`); `classify_conflict(carrier, target, width) -> Literal["CO_CHANNEL","ADJACENT_CHANNEL"]` | stdlib | 55 |
| `tools.py` | The 3 tool **bodies** (free functions, signature-only): `search_intervention_history(...)`, `get_device_lifecycle_summary(...)`, `correlate_sector_interference(...)`. Each takes `Settings` injected, calls `storage` + `sanitize` + `correlation`, returns JSON-safe dict. | `models`, `storage`, `sanitize`, `correlation` | 130 |
| `shim_webui.py` | `class Tools` with `async def` methods that delegate 1:1 to `tools.py` (with `__event_emitter__` accepted + ignored). Plus `TOOL_SPECS: list[dict]` matching openchat's `webui.db` `specs` JSON schema and `TOOL_META: dict` matching `manifest`. | `tools` | 70 |

**Total new src code**: ~530 LOC.

### Dependency arrows (no cycles)

```
src/nora/server.py            → nora.intervention_memory.tools (registers 3 @mcp.tool)
src/nora/intervention_memory/__init__.py → models, storage, correlation, sanitize, tools
src/nora/intervention_memory/tools.py    → storage, sanitize, correlation, models
src/nora/intervention_memory/storage.py  → models
src/nora/intervention_memory/sanitize.py → nora.sanitizer.Sanitizer
src/nora/intervention_memory/correlation.py → (stdlib only)
src/nora/intervention_memory/models.py   → pydantic
src/nora/intervention_memory/shim_webui.py → tools (delegates)
openchat deploy script (out of repo) → inspect.getsource(nora.intervention_memory.shim_webui.Tools)
```

`_AutoTraceMiddleware` records every `@mcp.tool` invocation for free — no middleware change.

## 5. Data model (mirrors spec §4, `extra="ignore"` everywhere)

### `InterventionMemoryRecord` (`BaseModel`, `model_config = ConfigDict(extra="ignore")`)

| Field | Type | Required? |
|---|---|---|
| `intervention_id` | `str` | YES (spec) |
| `timestamp_iso` | `str` (ISO 8601, validated via `datetime` parse-on-use) | YES (spec) |
| `timestamp_unix` | `int` (epoch seconds) | YES (spec) |
| `ticket_number` | `str` | YES (spec) |
| `target_ip` | `str` | YES (spec) — sanitized on tool-output serialise |
| `stage` | `Literal["PRE_DIAGNOSTIC","SPECTRUM_ANALYSIS","PRE_MIGRATION","SAFETY_ABORT","POST_MIGRATION_VERIFIED","POST_INTERVENTION"]` | YES (spec) |
| `record_name` | `str` | YES (spec) — sanitized |
| `status` | `Literal["COMPLETED","ABORTED","ACTION_REQUIRED","PENDING_VERIFICATION"]` | YES (spec) |
| `agent_name` | `str` | YES (spec) |
| `network_equipment` | `NetworkEquipmentBlock` | YES (spec) |
| `findings_and_dictamen` | `str` | YES (spec) — sanitized |
| `recommended_action` | `Optional[str] = None` | NO (v7 records lack this) — sanitized |
| `created_at` | `str` | YES (spec) |

### `NetworkEquipmentBlock` (nested)

| Field | Type | Required? |
|---|---|---|
| `target_ip` | `Optional[str] = None` | NO |
| `system_name` | `Optional[str] = None` | NO (sanitized) |
| `hardware_band` | `Optional[str] = None` | NO (v7 lacks this) |
| `carrier_frequency_mhz` | `Optional[float] = None` | NO |
| `total_provisioned_sms` | `Optional[int] = None` | NO |
| `active_online_sms_count` | `Optional[int] = None` | NO |
| `pre_existing_offline_sms_count` | `Optional[int] = None` | NO |
| `frame_utilization_dl_pct` | `Optional[float] = None` | NO |
| `frame_utilization_ul_pct` | `Optional[float] = None` | NO |
| `pre_existing_offline_subscribers` | `list[PreExistingOfflineSubscriber] = Field(default_factory=list)` | NO |

### `PreExistingOfflineSubscriber` (nested)
**`mac` vs `mac_address` divergence resolution (explore recommendation, accepted)**: declare the canonical spec name `mac: Optional[str] = None`. v8 prototype writes `mac_address` — that field survives via `model_config(extra="ignore")` (not promoted into `mac`, but does not raise). This means NORA's `InterventionMemoryRecord.network_equipment.pre_existing_offline_subscribers[i].mac` is populated for v7-schema records and `None` for v8 records; both shapes are accepted. A future spec-extension can add a `model_validator(mode="before")` to promote `mac_address` → `mac` if cross-version recall becomes a regression. Out of scope for this slice.

| Field | Type | Required? |
|---|---|---|
| `luid` | `Optional[int] = None` | NO |
| `mac` | `Optional[str] = None` (canonical; `mac_address` tolerated via `extra="ignore"`) | NO |
| `ip` | `Optional[str] = None` | NO (sanitized) |
| `uptime` | `Optional[str] = None` | NO |
| `note` | `Optional[str] = None` | NO (sanitized) |

## 6. Tool contracts (exact I/O)

### `search_intervention_history`
```python
def search_intervention_history(
    settings: Settings,
    target_ip: Optional[str] = None,
    ticket_number: Optional[str] = None,
    stage: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 5,                    # cap; clamped to [1, 100]
) -> list[dict[str, Any]]
```
- **Storage path**: `Settings.nora_interventions_dir` (env `NORA_INTERVENTIONS_DIR`).
- **Filenames**: glob `*.json`; tolerant read (try/except per file → log warning → skip).
- **Filter precedence**: `target_ip` (exact strip match on `record.target_ip` — exact, not substring; matches prototype's `!=` check), `ticket_number` (substring containment on `record.ticket_number`), `stage` (case-insensitive equality), `keyword` (substring containment on `json.dumps(record).lower()`, prototype behaviour).
- **Keyword I/O cap**: read AT MOST `Settings.nora_interventions_keyword_search_max_records` files even if `limit=5` early-returns; logs WARNING when the cap kicks in. Default 1000 (`NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS`).
- **Sort**: `timestamp_unix` DESC; slice `[:limit]`.
- **Output**: list of `InterventionMemoryRecord.model_dump(mode="json")`, every free-text string passed through `Sanitizer.sanitize` (`record_name`, `findings_and_dictamen`, `recommended_action`, `agent_name`, `network_equipment.system_name`, `network_equipment.hardware_band`, every `note`).
- **Empty dir / no matches**: returns `[]`.

### `get_device_lifecycle_summary`
```python
def get_device_lifecycle_summary(
    settings: Settings,
    target_ip: str,                    # required
) -> dict[str, Any]
```
- **Algorithm**: `search(target_ip=target_ip, limit=20)`. If empty → `{"status": "NO_HISTORY_FOUND", "target_ip": target_ip}`. Otherwise returns:
  ```python
  {
    "status": "SUCCESS",
    "target_ip": target_ip,                       # sanitized
    "total_recorded_interventions": len(records),
    "associated_tickets": sorted(set(r.ticket_number for r in records if r.ticket_number)),
    "stages_recorded": [r.stage for r in records],   # already-sorted by ts
    "latest_intervention": records[0].model_dump(mode="json"),  # sanitized
    "known_pre_existing_offline_subscribers": <from the most-recent PRE_DIAGNOSTIC record's network_equipment.pre_existing_offline_subscribers, sanitized; [] if none>
  }
  ```
- `limit` differs from search-tool default (`20` here, `5` in `search_intervention_history`) to give the lifecycle summary enough history for the `PRE_DIAGNOSTIC` extraction to find an offline-SM list.

### `correlate_sector_interference`
```python
def correlate_sector_interference(
    settings: Settings,
    tower_name: str,                       # required
    target_frequency_mhz: float,            # required
    channel_width_mhz: float = 20.0,        # default 20
) -> dict[str, Any]
```
- **Algorithm**:
  1. Read up to `Settings.nora_interventions_correlate_scan_limit` records (default 50) via `search(limit=...)`. Once cap is hit, log WARNING.
  2. For each record with `network_equipment.system_name` AND `network_equipment.carrier_frequency_mhz is not None`:
     - Tower match: `tower_name.lower() in network_equipment.system_name.lower()` (substring, explore recommendation Q5).
     - Frequency match: `abs(carrier - target_frequency_mhz) < channel_width_mhz`.
     - Classify: `CO_CHANNEL` if `carrier == target_frequency_mhz`, else `ADJACENT_CHANNEL`.
  3. Output:
     ```python
     {
       "tower_name": tower_name,
       "proposed_frequency_mhz": target_frequency_mhz,
       "channel_width_mhz": channel_width_mhz,
       "is_frequency_clear_on_tower": len(conflicts) == 0,
       "detected_conflicts": [
         {
           "neighbor_device": <system_name, sanitized>,
           "neighbor_ip": <target_ip, sanitized>,
           "carrier_frequency_mhz": <float>,
           "frequency_delta_mhz": <abs(carrier - target_frequency_mhz)>,
           "potential_conflict": "CO_CHANNEL" | "ADJACENT_CHANNEL",
         }
       ],
     }
     ```
- **Tower matching caveat (documented in module docstring)**: substring match handles `TWR-ISABEL-5GHZ-A` cleanly but will false-match if `tower_name == "A"` matches every `*-A` AP. Future enhancement: structured `tower` field in records (out of scope).

### MCP wrappers (`server.py`)

Each is a 3-line body delegating to `tools.py`:

```python
@mcp.tool
def search_intervention_history(
    target_ip: Optional[str] = None,
    ticket_number: Optional[str] = None,
    stage: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """[docstring mirrors spec §5.1.query + the keyword addition]"""
    settings, _ = get_runtime_state()
    return intervention_memory_search(
        settings=settings, target_ip=target_ip, ticket_number=ticket_number,
        stage=stage, keyword=keyword, limit=limit,
    )
```

(Same shape for the other 2.) `_AutoTraceMiddleware` records every call automatically.

### `shim_webui.py` (openchat deploy)

```python
class Tools:
    INTERVENTIONS_DIR = ""  # openchat deploy script overwrites with /app/backend/data/interventions|/home/alex/openchat/data/interventions
    KEYWORD_SEARCH_MAX_RECORDS = 1000
    CORRELATE_SCAN_LIMIT = 50

    async def search_intervention_history(self, target_ip=None, ticket_number=None,
                                          stage=None, keyword=None, limit=5, **kwargs):
        return _call(target_ip=target_ip, ticket_number=ticket_number, ...)
    # ... same for summarise_lifecycle / correlate_sector_interference
```

`inspect.getsource(Tools)` → rendered text goes into `webui.db` `tool.content`. openchat writes the next deploy script that does this (out of scope for NORA's slice — NORA ships the shim, openchat ships the deploy).

## 7. Integration points

| File | Touch type | What changes (apply phase, NOT now) |
|---|---|---|
| `src/nora/server.py` | Modified | Register 3 `@mcp.tool` functions using `from nora.intervention_memory.tools import (...)`; each delegates to the pure function. No middleware change. Re-export the 3 names in `__all__` for test discoverability. |
| `src/nora/config.py` | Extended | Add: `nora_interventions_dir: Path = Path("./var/interventions/")`, `nora_interventions_keyword_search_max_records: int = 1000`, `nora_interventions_correlate_scan_limit: int = 50`. Existing `extra="ignore"` (line 49) keeps existing callers source-compatible. |
| `src/nora/sanitizer.py` | Unchanged | Existing `Sanitizer.sanitize(...)` already masks private IPv4 (10/8, 172.16/12, 192.168/16, 127/8) + MAC (colon or hyphen delimited) + serial + hostname aliases. Confirmed covers every free-text field in tool output (`record_name`, `findings_and_dictamen`, `recommended_action`, `agent_name`, `system_name`, `note`, `target_ip`). Document the contract in `intervention_memory/sanitize.py:module-docstring`. |
| `src/nora/core/session_journal.py` | Borrowed (re-implemented in `storage.py`) | Re-implement the `_load_or_create_or_recover` per-file try/except pattern (lines 452–469) in `intervention_memory/storage.py`: `try: json.load + model_validate except (json.JSONDecodeError, ValidationError): logger.warning(filename); continue`. NORA does NOT use `SessionJournal` because it reads other-process files. |
| `pyproject.toml` | Possibly raised | Verify coverage threshold (85% in `openspec/config.yaml:116`) after new modules land. Each new module has ≥5 unit tests; expected coverage ≥90% on `intervention_memory/*`. If 85% is hit, no bump needed. `pytest-asyncio` is NOT introduced — tool bodies are sync (FastMCP handles wrapping); the shim's `async def` methods match openchat's in-process execution convention. `[tool.pytest.ini_options]` stays unchanged. |
| `.env.example` | Extended | New section with `NORA_INTERVENTIONS_DIR=./var/interventions/` + comment explaining the operator wires the real `.22` path (e.g., `/app/backend/data/interventions` if docker-mounted, else `/home/alex/openchat/data/interventions`); plus `NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS=1000` and `NORA_INTERVENTIONS_CORRELATE_SCAN_LIMIT=50` commented-out as overrides. **Do NOT commit a real production path.** |

## 8. Open decisions (close 6 from explore.md)

| Q | Decision | Rationale |
|---|---|---|
| **Q1** Transport (webui.db vs MCP) | **Espejo webui.db via `shim_webui.py` + MCP via `@mcp.tool`.** Both wrappers, single source of truth. | User preflight pick "Espejo webui.db". MCP remains available for any MCP client (LM Studio, etc.). Source-of-truth pattern prevents drift. |
| **Q2** Production path on `.22` | **Defer to operator via `NORA_INTERVENTIONS_DIR`. Default in `.env.example` is `./var/interventions/` (relative).** | SCOPE.md §2 + explore.md: real paths NEVER enter the repo. Operator wires `NORA_INTERVENTIONS_DIR=/app/backend/data/interventions` (or host mount) in `.env` on `.22`. The Settings field is `Path` so the operator passes absolute or relative — both work. |
| **Q3** Read-only enforcement | **HARD RULE. AST-level test + comment in `__init__.py` + tooltip in server.py docstring.** | User preflight lock-in. Test scans `src/nora/intervention_memory/` for any `open(... "w" / "a" / "x")`, `Path.write_text`, `Path.unlink`, `os.replace`, `shutil.rmtree`, `os.remove`; failures list offenders by file:line. Mirrors `tests/test_driver_airgap.py` AST pattern. |
| **Q4** Schema governance | **NORA MIRRORS. openchat owns the writer; NORA's `InterventionMemoryRecord` is a read-time adapter (`extra="ignore"`).** | User preflight lock-in. Forward-compat cost of NORA being authoritative is unacceptable (every operator upgrade must coordinate with openchat). Mirror approach means v7 + v8 + future schema drift all read without NORA changes. |
| **Q5** `tower_name` matching | **Substring `lower()` match on `system_name`.** Document the false-match caveat (e.g., `tower_name="A"` matches `*-A`). Structured `tower` field is out of scope (future enhancement). | User preflight lock-in. Acceptable for this slice per prototype precedent. Documented limitation belongs in the tool docstring + spec scenario. |
| **Q6** Limit defaults | **Search `limit=5`, correlate scan `limit=50`, keyword I/O cap `1000`. All env-overridable.** | Matches prototype behaviour; matches spec defaults. `.env.example` documents overrides. |

## 9. Risks (explore's 8 + new ones surfaced by design)

| # | Risk | Likelihood | Severity | Mitigation |
|---|---|---|---|---|
| R1 | Production data leak (private IPv4 / MAC literals in tool output) | Medium | HIGH | Every free-text field on every record passes through `Sanitizer.sanitize(...)` before return. Property test: every fixture with private IP/MAC yields aliases in tool output. Re-sanitize on read (defence in depth) — same pattern as `session_journal._resanitize_state`. |
| R2 | Future contributor adds a write tool (regression of read-only hard rule) | Medium | HIGH | AST lint test (`test_no_writes.py`): any `open(... "w" / "a" / "x")`, `Path.write_text`, `Path.unlink`, `os.replace`, `os.remove`, `shutil.rmtree` under `src/nora/intervention_memory/` FAILS the build. Module docstring + `__init__.py` + `tools.py` docstring repeat the rule. |
| R3 | Schema drift across v7 / v8 records (`recommended_action`, `hardware_band`, `mac` vs `mac_address`) | Medium | MEDIUM | `model_config = ConfigDict(extra="ignore")`; every non-required field is `Optional[...] = None`. Fixture with one v7 + one v8 record parses both. |
| R4 | Empty / missing data dir on first boot | Low | MEDIUM | `storage.py` returns `[]` if dir missing; `get_device_lifecycle_summary` returns `{"status":"NO_HISTORY_FOUND", ...}`. Do NOT create dir (NORA doesn't own it). |
| R5 | Corrupt / partially-written record (openchat writer crashes mid-write) | Medium | MEDIUM | Per-file `try/except (json.JSONDecodeError, ValidationError)` → `logger.warning(filename); continue`. Unit test: `}.invalid.json` is skipped without raising. |
| R6 | Keyword search I/O blowup (10k-record dir, `keyword="interference"`) | Low–Med | MEDIUM | `nora_interventions_keyword_search_max_records` env cap (default 1000). Settings field; `.env.example` documents. WARNING logged when cap kicks in. |
| R7 | Tool output size with v8 `pre_existing_offline_subscribers` | Low | LOW | Soft cap: docstring notes ~10 KB / record worst case. Out of scope to enforce a hard byte cap. |
| R8 | Concurrent appenders in openchat | Low | LOW | NORA doesn't write → no lock needed. Tolerant-read handles a torn write. `intervention_id` UUID6 suffix is openchat's filename disambiguator. |
| R9 | **NEW — drift between MCP-exposed tools and webui.db-registered Tools-class** | Medium | MEDIUM | Source-of-truth architecture: `shim_webui.py.Tools` delegates to the same `tools.py` functions MCP wraps. One code change updates both surfaces. The next openchat deploy renders `inspect.getsource(ShimTools)` into `webui.db`'s `tool.content`. Drift requires intentional re-deploy of openchat with stale shim code. |
| R10 | **NEW — JSON pointer path mismatch inside nested `latest_intervention`** | Low | LOW | `InterventionMemoryRecord.model_dump(mode="json")` is recursive; all nested lists / dicts share the same sanitization pass. Unit test with deeply nested record. |
| R11 | **NEW — coverage 85% gate after new modules** | Low | LOW | `models` 100% (table-driven parse tests), `storage` ≥85% (per-file + edge cases), `correlation` 100% (pure), `sanitize` 100% (mirrors existing), `tools` ≥80% (3 tool bodies). Estimate slice coverage ≥88%; gate stays. |

## 10. Testing strategy (Strict TDD — `apply` phase does RED → GREEN → REFACTOR per task)

Test files (under `tests/intervention_memory/`):

| File | Covers | RED → GREEN anchor |
|---|---|---|
| `test_models.py` | Pydantic parse of v7 + v8 fixture JSON; `extra="ignore"` tolerates extra keys; `Optional` fields default `None` when missing; `stage` / `status` Literal rejection | Fixture pair: `tests/intervention_memory/fixtures/v7_baseline.json`, `tests/intervention_memory/fixtures/v8_pre_migration.json`. Assert both parse into same `InterventionMemoryRecord` class. |
| `test_storage.py` | Glob `*.json` under tmp dir; skip-on-error for `}.invalid.json` and `validation_failure.json`; empty dir returns `[]`; missing dir returns `[]` (does NOT create it); sort by `timestamp_unix` DESC | Use `tmp_path` fixture; write 3 valid + 2 invalid + 0-empty cases. |
| `test_correlation.py` | `match_tower`: substring case-insensitive positive + negative + `tower_name in system_name` boundary case ("A" matches nothing if exact-match needed). `classify_conflict`: equal carrier → `CO_CHANNEL`; `0 < delta < width` → `ADJACENT_CHANNEL`; `delta == width` → not a conflict (strict `<`). | Pure function tests, no fixtures. |
| `test_sanitize.py` | Fixture record with private IPv4 + MAC + hostname in `record_name`, `findings_and_dictamen`, `system_name`, MAC in `network_equipment.pre_existing_offline_subscribers[0].mac`. Assert output has `RADIO_NODE_*` / `SWITCH_ACC_*` / `HOST_*` aliases instead of literals. | Mirror `tests/test_sanitizer.py` patterns. |
| `test_tools.py` | End-to-end `@mcp.tool` invocation with `set_runtime_state(settings=...)` against a `tmp_path` of 3 fixture records. Assert: `search(limit=5)` returns sorted list; `search(keyword=...)` returns subset; `search(limit=100)` clamps; `get_device_lifecycle_summary` finds `PRE_DIAGNOSTIC` and extracts `known_pre_existing_offline_subscribers`; `correlate_sector_interference` finds tower substring match + classifies CO vs ADJACENT. | Import the 3 from `src.nora.server` (the actual MCP wrappers), not from `tools.py` directly — proves the wiring. |
| `test_no_writes.py` | AST scan of every `.py` under `src/nora/intervention_memory/`. Fail with file:line on: `open(... "w" / "a" / "x")`, `Path.write_text`, `Path.write_bytes`, `Path.unlink`, `os.replace`, `os.remove`, `shutil.rmtree`, `os.removedirs`, `os.makedirs`. Whitelist: `open(... "r")` reads. | Mirror `tests/test_driver_airgap.py` AST walker. Use a regex on read-text fallback for AST-edge-cases (e.g., `getattr(path, "write_text", None)(...)`). |

Plus borrowed patterns from `tests/core/test_session_journal.py` for tolerant-read (`test_recovery_from_corrupt` analogue) and `tests/test_driver_airgap.py` for the AST scaffold.

**No `pytest-asyncio`.** Tool bodies are sync functions; FastMCP wraps them. The shim's `async def` is registered into Open WebUI in-process and never executes under pytest.

## 11. Deliverables forecast (LOC against 400-line budget)

| File | LOC est. | Type |
|---|---|---|
| `src/nora/intervention_memory/__init__.py` | 15 | New |
| `src/nora/intervention_memory/models.py` | 90 | New |
| `src/nora/intervention_memory/storage.py` | 110 | New |
| `src/nora/intervention_memory/sanitize.py` | 60 | New |
| `src/nora/intervention_memory/correlation.py` | 55 | New |
| `src/nora/intervention_memory/tools.py` | 130 | New |
| `src/nora/intervention_memory/shim_webui.py` | 70 | New |
| `tests/intervention_memory/test_models.py` | 110 | New |
| `tests/intervention_memory/test_storage.py` | 95 | New |
| `tests/intervention_memory/test_correlation.py` | 70 | New |
| `tests/intervention_memory/test_sanitize.py` | 80 | New |
| `tests/intervention_memory/test_tools.py` | 140 | New |
| `tests/intervention_memory/test_no_writes.py` | 75 | New |
| `tests/intervention_memory/fixtures/v7_baseline.json` | 35 | Fixture |
| `tests/intervention_memory/fixtures/v8_pre_migration.json` | 50 | Fixture |
| `tests/intervention_memory/fixtures/invalid.json` | 3 | Fixture |
| `src/nora/server.py` | +28 (3 tool wrappers + import + `__all__`) | Modified |
| `src/nora/config.py` | +9 (3 Settings fields + section comment) | Modified |
| `.env.example` | +12 (3 keys + comments) | Modified |

**Total authored additions**: ~1237 LOC. **Total modifications**: ~49 LOC. **Total diff for `git diff --stat`**: ~1286 LOC.

### 400-line PR-review budget analysis
- Single-pr risk: **HIGH** (1286 >> 400).
- Chained PRs: **YES** (orchestrator should pre-confirm).
  - **PR1 — pure logic + tests**: new package `intervention_memory/` (7 files + 6 test files + 2 fixtures) = ~1100 LOC. Review focus: schema tolerance + sanitization boundary + read-only guarantee.
  - **PR2 — NORA wiring**: `src/nora/server.py` (+28 LOC) + `src/nora/config.py` (+9 LOC) + `.env.example` (+12 LOC) = ~49 LOC. Review focus: pure-function delegation, Settings threading, `.env.example` hygiene.
  - **PR3 (out of repo, openchat)**: openchat writes the deploy script that does `inspect.getsource(nora.intervention_memory.shim_webui.Tools)` and pastes it into `webui.db`. Not NORA's diff.
- Recommend **`ask-on-risk`**: orchestrator surfaces "Single PR (1286 LOC, well over 400) vs chained (PR1 ~1100, PR2 ~49)" at the apply gate, default = chained.

## 12. Acceptance criteria (sdd-verify will run)

1. **RED-GREEN-REFACTOR trace**: every committed file in `src/nora/intervention_memory/` and every test in `tests/intervention_memory/` has a sibling RED test committed in the same logical task.
2. **`uv run pytest tests/intervention_memory/ -v` exits 0**; 6 test files × ≥5 tests each = ≥30 tests passing.
3. **`uv run pytest tests/intervention_memory/test_no_writes.py` exits 0** — proves the read-only AST guard. Add a throwaway `_poison_write.py` in a sandbox test to confirm the test correctly fails on a hand-injected write, then delete the poison file.
4. **`uv run pytest` global exits 0** (no breakage of existing 7 + nora + driver + session-journal test files).
5. **`uv run ruff check . && uv run ruff format --check .`** exits 0.
6. **`uv run mypy --strict src/nora`** exits 0 (new modules pass strict type-check; `pydantic` v2 `BaseModel` interfaces are well-typed).
7. **`uv run pytest --cov=src/nora --cov-report=term-missing --cov-fail-under=85`** — new modules ship at ≥85% line coverage (likely ≥90% per risk R11 forecast); gate stays.
8. **`src/nora/server.py` exports** the 3 new tools in `__all__`; `from nora.server import search_intervention_history, get_device_lifecycle_summary, correlate_sector_interference` succeeds.
9. **Sanitizer property test**: load a fixture record carrying one private IPv4 in `target_ip`, one MAC in `pre_existing_offline_subscribers[0].mac`, one hostname-style literal in `system_name`. Call `search_intervention_history` via the MCP wrapper. Assert the JSON response contains the `RADIO_NODE_*` / `SWITCH_ACC_*` / `HOST_*` aliases and ZERO original literals.
10. **End-to-end correlation test**: create 4 fixture records (2 on tower "TWR-ISABEL-A", 1 on tower "TWR-PASO-A", 1 on "TWR-ISABEL-B"). Call `correlate_sector_interference(tower_name="TWR-ISABEL", target_frequency_mhz=5760.0, channel_width_mhz=20.0)` with one fixture using `carrier_frequency_mhz=5760.0` (CO_CHANNEL) and another at `5770.0` (ADJACENT_CHANNEL). Assert 2 conflicts with correct classification.
11. **AST lint self-test** (sanity check before merge): inject `def _poison(): Path("/tmp/x").write_text("x")` into `intervention_memory/storage.py` locally; `pytest tests/intervention_memory/test_no_writes.py` exits non-zero with the offending `("storage.py", N, "write_text")`. Revert before committing.
12. **No file outside `openspec/changes/phase3-intervention-memory-mcp/proposal.md` was modified in this `sdd-propose` phase.** All code-bearing diffs happen in `sdd-apply`.

## 13. Rollback plan

NORA's pure-read slice is reversible in three moves:

1. **Remove the package**: `git revert <merge>` (or `rm -rf src/nora/intervention_memory tests/intervention_memory`). Existing 7 `@mcp.tool` registrations untouched; FastMCP server boots without the 3 new tools.
2. **Revert wiring**: `git revert <server.py + config.py + .env.example>` step. Drop the 3 `Settings` fields (extra="ignore" means absence is safe) and the 3 server.py `@mcp.tool` registrations.
3. **If a deploy script in openchat already pasted the shim into `webui.db`**: re-run `deploy_v7_intervention_memory.py` (which `INSERT INTO tool (...) ON CONFLICT(id) DO UPDATE`) — the next openchat deploy overwrites the `webui.db` `tool` row to the pre-shim content. No orphan.

**No data loss risk**: NORA never wrote to the interventions dir. openchat's `intervention_memory_tool` keeps owning writes; its files are untouched by NORA's presence or absence.

## 14. Dependencies

- `pydantic` (already a transitive of `pydantic-settings>=2` per `pyproject.toml` line 28). No new runtime dep.
- Dev: `pytest>=8`, `pytest-cov>=5`, `pytest-timeout>=2.4`, `ruff>=0.6`, `mypy>=1.10` — all already pinned in `[dependency-groups]`. No new dev dep.
- Strict-TDD test runner: `uv run pytest --strict-markers --strict-config` per `openspec/config.yaml:74`; the new test files declare no new markers (no `asyncio` markers, no `slow` markers — all are plain `def` tests).

## 15. Capabilities (sdd-spec contract)

### New Capabilities
- `intervention-memory`: read-only persistence + correlation for NetOps intervention JSON records across `search_intervention_history`, `get_device_lifecycle_summary`, `correlate_sector_interference`. One full spec at `openspec/changes/phase3-intervention-memory-mcp/specs/intervention-memory/spec.md` covering: schema (v7/v8 tolerant), storage glob + skip-on-error, sanitization boundary on free-text fields, lifecycle summary derivation, correlation algorithm (substring tower match + frequency-delta classification), env-overridable caps, and the read-only hard rule.

### Modified Capabilities
- `nora-mcp-server`: one delta scenario per new tool (3 scenarios appended to the existing `mcp-server-tool-registration` requirement set), plus a cross-reference to `intervention-memory` for the sanitizer-boundary contract.

## Key Learnings (capture to engram at end)

1. Source-of-truth pattern (`tools.py` + 2 wrappers) prevents drift between MCP-exposed tools and webui.db-registered `Tools`-class shims — architectural rule for any future NORA feature that both surfaces must serve.
2. `extra="ignore"` + `Optional[...] = None` on every non-required field is the contract for forward/backward schema compatibility when one process reads files another process writes.
3. mac-vs-mac_address divergence is solved by declaring the canonical name and letting `extra="ignore"` tolerate the legacy alias — no model-time migration needed.
