# intervention-memory Specification

## Purpose

Defines the read-only on-disk NetOps intervention memory and correlation layer that
NORA exposes under `src/nora/intervention_memory/`. The package consumes JSON
records written by openchat's existing `intervention_memory_tool` (one
`{ticket}_{ip}_{stage}_{timestamp}.json` per intervention), parses them through
Pydantic `BaseModel`s with `extra="ignore"` for v7/v8 forward-backward
tolerance, walks every free-text field through `Sanitizer.sanitize(...)` on
read, and returns three correlation views: history search, per-device
lifecycle summary, and tower co/adjacent-channel interference detection. The
package is structurally read-only — no `open('w')`, no `Path.write_*`, no
`os.remove` — enforced by an AST scan under `tests/intervention_memory/test_no_writes.py`.
The same logic powers NORA's MCP wrappers (`@mcp.tool` in
`src/nora/server.py`) and the webui.db mirror shim
(`src/nora/intervention_memory/shim_webui.py`), preventing drift between the
two surfaces.

## Requirements

### Requirement: R1 — Data Model Tolerates v7 + v8 Records

The package SHALL provide Pydantic models `InterventionMemoryRecord`,
`NetworkEquipmentBlock`, and `PreExistingOfflineSubscriber`, each with
`model_config = ConfigDict(extra="ignore")`. Every non-required field SHALL
be declared `Optional[T] = None`. `PreExistingOfflineSubscriber.mac` SHALL
be the canonical field name; v8 records that write `mac_address` SHALL be
tolerated by `extra="ignore"` without renaming (the v8 alias does not
populate `mac`, but does not raise). Stage and status fields SHALL use
`Literal[...]` enums matching `NETOPS_INTERVENTION_MEMORY_MCP_SPEC.md` §4.

#### Scenario: v7 record without `recommended_action` parses

- GIVEN a v7 JSON fixture that omits `recommended_action`
- WHEN `InterventionMemoryRecord.model_validate(fixture)` runs
- THEN `record.recommended_action is None`
- AND no exception is raised

#### Scenario: v8 record with `mac_address` alias is tolerated

- GIVEN a v8 JSON fixture whose subscriber row writes `mac_address` instead of `mac`
- WHEN `PreExistingOfflineSubscriber.model_validate(fixture)` runs
- THEN no exception is raised
- AND `record.mac is None` (the alias is dropped, not promoted)

#### Scenario: invalid stage literal is rejected

- GIVEN a fixture with `"stage": "BOGUS_STAGE"`
- WHEN `InterventionMemoryRecord.model_validate(fixture)` runs
- THEN a `pydantic.ValidationError` is raised

### Requirement: R2 — Storage Layer Is Strictly Read-Only

No source file under `src/nora/intervention_memory/` SHALL invoke `open(...)`
with a writable mode (`"w"`, `"a"`, `"x"`, `"+"`), `Path.write_text`,
`Path.write_bytes`, `Path.unlink`, `os.replace`, `os.remove`,
`os.removedirs`, `os.makedirs`, or `shutil.rmtree`. The constraint SHALL be
enforced by an AST scan in `tests/intervention_memory/test_no_writes.py` that
walks every `.py` file under the package and fails the build on any match.
The package SHALL NOT create `Settings.nora_interventions_dir` if absent.
The write surface for `nora_interventions_dir` lives in the sibling
capability `intervention-writer` (see
`openspec/specs/intervention-writer/spec.md`); the reader package remains
read-only and MUST NOT import from the writer. One-way dependency direction
is preserved: writer → reader (for schema only), never reader → writer.
(Previously: R2 had no acknowledgement of the sibling writer capability;
this addition is a cross-reference, not a relaxation of the read-only
guarantee.)

#### Scenario: AST scan finds zero writable file calls under the package

- GIVEN every `.py` under `src/nora/intervention_memory/`
- WHEN `tests/intervention_memory/test_no_writes.py` runs its AST walker
- THEN the assertion passes
- AND the build exits 0

#### Scenario: an injected `Path.write_text` call breaks the build

- GIVEN a developer adds `Path("/tmp/x").write_text("x")` to `storage.py`
- WHEN `tests/intervention_memory/test_no_writes.py` runs
- THEN the test fails with the offending `(file, line, "write_text")` triple

#### Scenario: `open(..., "r")` reads are allowed

- GIVEN `storage.py` opens a file with `open(path, "r", encoding="utf-8")`
- WHEN the AST scan runs
- THEN the call is NOT flagged as a write

#### Scenario: the reader does not import from the writer sibling

- GIVEN every `.py` under `src/nora/intervention_memory/` except its `__init__.py`
- WHEN a static grep scans for `from nora.intervention_writer`, `import nora.intervention_writer`, `from nora.server`, or `import nora.server`
- THEN zero matches are found (one-way dep writer → reader, never the reverse)
### Requirement: R3 — Tolerant Read Skips Corrupt Or Partial Files

On any per-file `json.JSONDecodeError` or `pydantic.ValidationError`, the
storage layer SHALL log a `WARNING` containing the filename and continue
without raising. The storage layer SHALL NOT create the interventions
directory if absent; it SHALL return an empty list. On an empty directory,
the storage layer SHALL return an empty list.

#### Scenario: corrupt JSON file is skipped without raising

- GIVEN a tmp dir with three valid records and one file named `}.invalid.json` containing `{not json`
- WHEN `load_records(dir)` runs
- THEN three records are returned
- AND a WARNING log line names `}.invalid.json`
- AND no exception propagates

#### Scenario: pydantic ValidationError is skipped without raising

- GIVEN a tmp dir with one valid record and one file that parses as JSON but fails `InterventionMemoryRecord` validation
- WHEN `load_records(dir)` runs
- THEN one record is returned
- AND a WARNING log line names the failing file

#### Scenario: missing directory returns empty results

- GIVEN `Settings.nora_interventions_dir` points at a path that does not exist
- WHEN `load_records(dir)` runs
- THEN `[]` is returned
- AND the directory is NOT created on disk

### Requirement: R4 — `search_intervention_history` Tool Contract

The package SHALL provide `search_intervention_history(settings,
target_ip=None, ticket_number=None, stage=None, keyword=None, limit=5)`
returning `list[dict[str, Any]]` of at most `limit` records sorted by
`timestamp_unix` DESC. Filters SHALL apply in this order: `target_ip`
(exact strip match on `record.target_ip`), `ticket_number` (substring
containment on `record.ticket_number`), `stage` (case-insensitive equality),
`keyword` (substring containment on `json.dumps(record).lower()`). Every
free-text field in every returned record SHALL pass through
`Sanitizer.sanitize(...)`. Empty results SHALL be `[]`.

#### Scenario: target_ip exact match returns one record

- GIVEN three fixture records with `target_ip` values `["10.0.0.4", "10.0.0.5", "10.0.0.5"]`
- WHEN `search_intervention_history(settings, target_ip="10.0.0.5")` runs
- THEN two records are returned, both with `target_ip == "10.0.0.5"`

#### Scenario: keyword substring match uses json.dumps lowering

- GIVEN one record whose `findings_and_dictamen` contains "interference observed"
- WHEN `search_intervention_history(settings, keyword="interference")` runs
- THEN the record is in the returned list

#### Scenario: results are sorted by timestamp_unix descending

- GIVEN three records with timestamps `[100, 300, 200]`
- WHEN `search_intervention_history(settings)` runs
- THEN the returned order is `[300, 200, 100]`

#### Scenario: limit clamps the result set

- GIVEN five matching records and `limit=2`
- WHEN `search_intervention_history(settings, limit=2)` runs
- THEN exactly two records are returned

#### Scenario: free-text fields are sanitized in output

- GIVEN a record whose `record_name` contains `10.53.12.4`
- WHEN `search_intervention_history(settings)` returns the record
- THEN `record["record_name"]` does NOT contain `10.53.12.4`
- AND it contains a `RADIO_NODE_*` alias

### Requirement: R5 — `get_device_lifecycle_summary` Tool Contract

The package SHALL provide `get_device_lifecycle_summary(settings, target_ip)`
returning `dict[str, Any]`. The algorithm SHALL call
`search_intervention_history(settings, target_ip=target_ip, limit=20)`. If
the result is empty, the function SHALL return
`{"status": "NO_HISTORY_FOUND", "target_ip": target_ip}`. Otherwise it SHALL
return `{"status": "SUCCESS", "target_ip": target_ip,
"total_recorded_interventions": N, "associated_tickets": [...],
"stages_recorded": [...], "latest_intervention": {...},
"known_pre_existing_offline_subscribers": [...]}` with
`known_pre_existing_offline_subscribers` populated from the most-recent
`PRE_DIAGNOSTIC` record's `network_equipment.pre_existing_offline_subscribers`.

#### Scenario: no matching records returns NO_HISTORY_FOUND

- GIVEN an empty interventions directory
- WHEN `get_device_lifecycle_summary(settings, target_ip="10.0.0.99")` runs
- THEN the returned dict equals `{"status": "NO_HISTORY_FOUND", "target_ip": "10.0.0.99"}`

#### Scenario: matching records return SUCCESS with all six fields

- GIVEN two records with `target_ip == "10.0.0.4"`, stages `PRE_DIAGNOSTIC` and `POST_INTERVENTION`, two distinct tickets
- WHEN `get_device_lifecycle_summary(settings, target_ip="10.0.0.4")` runs
- THEN the dict has `status == "SUCCESS"`
- AND `total_recorded_interventions == 2`
- AND `associated_tickets` contains both ticket strings
- AND `stages_recorded` is ordered by timestamp descending
- AND `latest_intervention` is the most recent record's dump

#### Scenario: offline subscribers extracted from latest PRE_DIAGNOSTIC

- GIVEN two `PRE_DIAGNOSTIC` records for the same IP, the newer carrying `pre_existing_offline_subscribers = [{"luid": 7, "mac": "aa:bb:cc:dd:ee:01"}]`
- WHEN `get_device_lifecycle_summary(settings, target_ip=...)` runs
- THEN `known_pre_existing_offline_subscribers` contains the newer record's subscriber list (sanitized)

### Requirement: R6 — `correlate_sector_interference` Tool Contract

The package SHALL provide `correlate_sector_interference(settings, tower_name,
target_frequency_mhz, channel_width_mhz=20.0)` returning `dict[str, Any]`.
The algorithm SHALL walk recent records (bounded by
`Settings.nora_interventions_correlate_scan_limit`, default 50) and, for
each record whose `network_equipment.system_name` AND
`network_equipment.carrier_frequency_mhz` are set, apply tower match
`tower_name.lower() in network_equipment.system_name.lower()` and frequency
match `abs(carrier - target_frequency_mhz) < channel_width_mhz`. Each
matching neighbor SHALL be classified `CO_CHANNEL` if `carrier ==
target_frequency_mhz`, otherwise `ADJACENT_CHANNEL`.

#### Scenario: equal carrier classifies CO_CHANNEL

- GIVEN a record with `carrier_frequency_mhz == 5760.0` and `system_name == "TWR-ISABEL-5GHZ-A"`
- WHEN `correlate_sector_interference(settings, tower_name="TWR-ISABEL", target_frequency_mhz=5760.0, channel_width_mhz=20.0)` runs
- THEN `detected_conflicts` contains one entry with `potential_conflict == "CO_CHANNEL"`
- AND `frequency_delta_mhz == 0.0`

#### Scenario: nearby carrier classifies ADJACENT_CHANNEL

- GIVEN a record with `carrier_frequency_mhz == 5770.0` and `system_name == "TWR-ISABEL-5GHZ-B"`
- WHEN the correlate tool runs with `target_frequency_mhz=5760.0, channel_width_mhz=20.0`
- THEN `detected_conflicts` contains one entry with `potential_conflict == "ADJACENT_CHANNEL"`

#### Scenario: tower substring mismatch returns zero conflicts

- GIVEN a record with `system_name == "TWR-PASO-5GHZ-A"`
- WHEN the correlate tool runs with `tower_name="TWR-ISABEL"`
- THEN `detected_conflicts == []`
- AND `is_frequency_clear_on_tower is True`

#### Scenario: result is sanitized

- GIVEN a neighbor `system_name` contains a private IPv4 literal
- WHEN the correlate tool returns
- THEN the literal is replaced by a `RADIO_NODE_*` alias in `neighbor_device`

### Requirement: R7 — Keyword Search Is Bounded By I/O Cap

The package SHALL cap the keyword-search I/O at
`Settings.nora_interventions_keyword_search_max_records` (default 1000).
When the cap kicks in, a `WARNING` SHALL be logged naming the cap value and
the search SHALL stop reading further files (records already loaded remain
filter-eligible).

#### Scenario: cap is enforced and warning logged

- GIVEN a tmp dir with 1500 JSON records and `nora_interventions_keyword_search_max_records=1000`
- WHEN `search_intervention_history(settings, keyword="interference")` runs
- THEN at most 1000 files are read (verified via monkeypatch counter)
- AND a WARNING log line mentions the cap value

#### Scenario: under the cap, no warning is logged

- GIVEN a tmp dir with 5 JSON records and the default cap of 1000
- WHEN the keyword search runs
- THEN no cap-related WARNING is emitted

### Requirement: R8 — Configuration Surface

`Settings` SHALL expose three new fields, all env-overridable, all with
sane defaults: `nora_interventions_dir: Path` (env
`NORA_INTERVENTIONS_DIR`, default `./var/interventions/`),
`nora_interventions_keyword_search_max_records: int` (env
`NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS`, default 1000), and
`nora_interventions_correlate_scan_limit: int` (env
`NORA_INTERVENTIONS_CORRELATE_SCAN_LIMIT`, default 50). The default
`nora_interventions_dir` SHALL be relative — `.env.example` SHALL document
how the operator wires the production path on `.22`; no production path
SHALL enter the repository.

#### Scenario: env var overrides default interventions_dir

- GIVEN `NORA_INTERVENTIONS_DIR=/tmp/custom/` in the process environment
- WHEN `Settings(_env_file=None)` is constructed
- THEN `settings.nora_interventions_dir == Path("/tmp/custom/")`

#### Scenario: defaults apply with no env override

- GIVEN no `NORA_INTERVENTIONS_*` env vars set
- WHEN `Settings(_env_file=None)` is constructed
- THEN `nora_interventions_dir == Path("./var/interventions/")`
- AND `nora_interventions_keyword_search_max_records == 1000`
- AND `nora_interventions_correlate_scan_limit == 50`

### Requirement: R9 — Sanitizer Integration Boundary

The package SHALL provide `sanitize_record_payload(record:
InterventionMemoryRecord, sanitizer: Sanitizer) -> dict` that walks the
record's free-text fields through `sanitizer.sanitize(...)`. Free-text
fields SHALL include: `record_name`, `findings_and_dictamen`,
`recommended_action`, `agent_name`, `network_equipment.system_name`,
`network_equipment.hardware_band`, and every `note` inside
`network_equipment.pre_existing_offline_subscribers`. The
`network_equipment` dict and the `pre_existing_offline_subscribers` list
SHALL be recursively walked. Structured top-level fields (`intervention_id`,
`timestamp_unix`, `stage`, `status`, `ticket_number`, `target_ip`,
`created_at`, `timestamp_iso`) SHALL bypass per the existing `session-journal`
R6 contract.

#### Scenario: private IPv4 in record_name is masked

- GIVEN a record whose `record_name == "investigation at 10.0.0.5"`
- WHEN `sanitize_record_payload(record, sanitizer)` runs
- THEN the returned `record_name` contains a `RADIO_NODE_*` alias
- AND it does NOT contain `10.0.0.5`

#### Scenario: MAC in subscriber note is masked

- GIVEN a record whose first offline subscriber `note == "dormant, mac aa:bb:cc:dd:ee:01"`
- WHEN the sanitizer helper runs
- THEN the returned `note` contains a `SWITCH_ACC_*` alias
- AND it does NOT contain `aa:bb:cc:dd:ee:01`

#### Scenario: structured top-level fields bypass sanitization

- GIVEN a record whose `intervention_id == "INT-1-10.0.0.5-1234567-XYZ"` (contains an IP-shaped substring)
- WHEN the sanitizer helper runs
- THEN the returned `intervention_id` is byte-identical to the original

### Requirement: R10 — webui.db Mirror Shim

The package SHALL provide `src/nora/intervention_memory/shim_webui.py`
exporting `class Tools` whose `async def` methods (`search_intervention_history`,
`get_device_lifecycle_summary`, `correlate_sector_interference`) delegate
1:1 to the same library functions as the `@mcp.tool` registrations. The
shim SHALL accept and ignore a `__event_emitter__` keyword. The openchat
deploy script SHALL be able to consume the shim via
`inspect.getsource(Tools)` to render into a `webui.db` tool entry. The
dependency direction SHALL be one-way: `shim_webui.py` imports from
`nora.intervention_memory.tools`; production modules SHALL NOT import from
`shim_webui`.

#### Scenario: shim `Tools` class exists and exposes three async methods

- GIVEN `from nora.intervention_memory.shim_webui import Tools`
- WHEN `inspect.getsource(Tools)` runs
- THEN the source contains `async def search_intervention_history`
- AND `async def get_device_lifecycle_summary`
- AND `async def correlate_sector_interference`

#### Scenario: shim methods delegate to the library function (no drift)

- GIVEN a monkeypatched `nora.intervention_memory.tools.search_intervention_history` that returns a sentinel `{"delegated": True}`
- WHEN `await Tools().search_intervention_history(target_ip="10.0.0.5")` runs
- THEN the patched function is called exactly once with `target_ip="10.0.0.5"`
- AND the result equals `{"delegated": True}`

#### Scenario: production modules do not import from the shim

- GIVEN every `.py` under `src/nora/intervention_memory/` except `shim_webui.py`
- WHEN a static grep scans for `from nora.intervention_memory.shim_webui`
- THEN zero matches are found

### Requirement: R11 — Strict TDD Coverage And Module Isolation

The package SHALL achieve at least 85% line coverage on the
`intervention_memory` package (per `openspec/config.yaml:116`). The package
SHALL NOT import any test-only code (`pytest`, `monkeypatch`, `_pytest`) in
production modules. The `tests/intervention_memory/` directory SHALL
contain at least six test files covering `models`, `storage`,
`correlation`, `sanitize`, `tools`, and the no-writes AST guard.

#### Scenario: coverage gate is met

- GIVEN `uv run pytest --cov=src/nora/intervention_memory --cov-fail-under=85`
- WHEN the test suite runs
- THEN the command exits 0
- AND the reported coverage for `src/nora/intervention_memory/` is at least 85%

#### Scenario: production modules do not import pytest

- GIVEN every `.py` under `src/nora/intervention_memory/` except `__init__.py`
- WHEN a static grep scans for `import pytest`, `from pytest`, `import monkeypatch`, or `from _pytest`
- THEN zero matches are found in production code

## Scenarios

This section consolidates the 32 scenarios above for verifier discoverability.
Each scenario maps 1:1 to a pytest function under
`tests/intervention_memory/`.

| Requirement | Scenario | Test file |
|---|---|---|
| R1 | v7 record parses without `recommended_action` | `test_models.py` |
| R1 | v8 record with `mac_address` alias is tolerated | `test_models.py` |
| R1 | invalid stage literal is rejected | `test_models.py` |
| R2 | AST scan finds zero writable file calls | `test_no_writes.py` |
| R2 | injected `Path.write_text` breaks the build | `test_no_writes.py` |
| R2 | `open(..., "r")` reads are allowed | `test_no_writes.py` |
| R3 | corrupt JSON file is skipped | `test_storage.py` |
| R3 | pydantic ValidationError is skipped | `test_storage.py` |
| R3 | missing directory returns empty results | `test_storage.py` |
| R4 | `target_ip` exact match returns one record | `test_tools.py` |
| R4 | keyword substring match uses `json.dumps` lowering | `test_tools.py` |
| R4 | results are sorted by `timestamp_unix` descending | `test_tools.py` |
| R4 | `limit` clamps the result set | `test_tools.py` |
| R4 | free-text fields are sanitized in output | `test_sanitize.py` |
| R5 | no matching records returns NO_HISTORY_FOUND | `test_tools.py` |
| R5 | matching records return SUCCESS with all six fields | `test_tools.py` |
| R5 | offline subscribers extracted from latest PRE_DIAGNOSTIC | `test_tools.py` |
| R6 | equal carrier classifies CO_CHANNEL | `test_correlation.py` |
| R6 | nearby carrier classifies ADJACENT_CHANNEL | `test_correlation.py` |
| R6 | tower substring mismatch returns zero conflicts | `test_correlation.py` |
| R6 | correlate result is sanitized | `test_sanitize.py` |
| R7 | keyword cap is enforced and warning logged | `test_tools.py` |
| R7 | under the cap, no warning is logged | `test_tools.py` |
| R8 | env var overrides default `interventions_dir` | (existing `tests/test_config.py`) |
| R8 | defaults apply with no env override | (existing `tests/test_config.py`) |
| R9 | private IPv4 in `record_name` is masked | `test_sanitize.py` |
| R9 | MAC in subscriber `note` is masked | `test_sanitize.py` |
| R9 | structured top-level fields bypass sanitization | `test_sanitize.py` |
| R10 | shim `Tools` exposes three async methods | `test_tools.py` |
| R10 | shim methods delegate without drift | `test_tools.py` |
| R10 | production modules do not import the shim | `test_no_writes.py` |
| R11 | coverage gate is met | (verify-report) |
| R11 | production modules do not import pytest | `test_no_writes.py` |

## Cross-References

- `nora-mcp-server` — the three new tools register on the global
  `FastMCP("nora")` instance; see `specs/nora-mcp-server/spec.md` R-NEW-1
  for the wiring contract.
- `session-journal` — the existing `_AutoTraceMiddleware` records every
  `@mcp.tool` invocation (including the three new tools) for free; the
  structured-bypass list in R6 of `session-journal` is reused by R9 here.
- `telemetry-sanitizer` — `Sanitizer.sanitize(...)` is the sole masker for
  every free-text field on every record; see `specs/telemetry-sanitizer/spec.md`.
- `secure-configuration` — the three new `Settings` fields inherit the
  locked-no-hardcoded-values rule; `.env.example` documents operator
  wiring with sanitized placeholders.
