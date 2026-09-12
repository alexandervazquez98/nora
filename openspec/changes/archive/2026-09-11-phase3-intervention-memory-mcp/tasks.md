# Tasks: phase3-intervention-memory-mcp — NetOps Persistent Memory & Correlation MCP

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Total LOC of additions | ~1302 |
| Total LOC of test additions | ~688 |
| Files added | 16 |
| Files modified | 3 |
| Estimated changed lines (additions + deletions) | ~1302 |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Delivery strategy | ask-on-risk |
| Chain strategy | pending |
| Decision needed before apply | Yes |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: stacked-to-main | feature-branch-chain | size-exception | pending
400-line budget risk: High

### Suggested Work Units

| Unit | Goal | Likely PR | Focused test command | Runtime harness | Rollback boundary |
|------|------|-----------|----------------------|-----------------|-------------------|
| 1 | Pure logic + tests + shim (no NORA wiring) | PR 1 | `uv run pytest tests/intervention_memory/ -v` | `uv run pytest --cov=src/nora/intervention_memory --cov-fail-under=85` | `git revert PR1` deletes `src/nora/intervention_memory/` + `tests/intervention_memory/`; NORA boot unaffected (no imports yet). |
| 2 | NORA wiring + config + integration gate | PR 2 | `uv run pytest tests/intervention_memory/ tests/test_server.py -v` | `uv run pytest --cov=src/nora --cov-fail-under=85` | `git revert PR2` reverts `server.py`/`config.py`/`.env.example`; Settings `extra="ignore"` keeps existing callers source-compatible. |

Rationale: total additions (~1302) are ~3.25× the 400-line PR-review budget. Splitting on logical boundary (PR1 = self-contained new package; PR2 = 3-line wiring + config) keeps each PR diff focused, reversible, and independently mergeable. PR1 review focus = schema tolerance + sanitization boundary + read-only AST guard; PR2 review focus = pure-function delegation + Settings threading + .env hygiene.

## Purpose

Break the phase3-intervention-memory-mcp slice into 26 atomic, strict-TDD tasks that land a read-only `intervention_memory` package on NORA plus the three MCP tool registrations on the global `mcp` instance. The package consumes openchat's on-disk JSON records, sanitizes free-text fields at read time, and exposes `search_intervention_history`, `get_device_lifecycle_summary`, and `correlate_sector_interference` on both the FastMCP surface and the webui.db shim. The AST guard at `tests/intervention_memory/test_no_writes.py` enforces the read-only hard rule structurally so a future contributor cannot add a write tool without breaking the build.

Test runner: `uv run pytest --strict-markers --strict-config`. Each task follows RED → GREEN → REFACTOR. The apply phase will follow strict-tdd.md per task.

Group numbering follows the user's prompt. Tasks are ordered by dependency (storage depends on models; tools depends on storage + sanitize + correlation; wiring depends on tools + config). AST guard tasks (Group 6) land BEFORE Group 1-7 production code so the AST test is the first committed thing in the new package — strict-TDD invariant.

---

## Task 0.1 — ✅ DONE — Scaffold `intervention_memory` package skeleton + tests directory

- **Goal**: Create the empty package + test tree so the AST guard (Task 6.1) has a directory to scan.
- **Strict TDD**: RED → GREEN collapses because the scaffolding itself is the contract; the AST guard RED test (6.1) is the first consumer. REFACTOR: ensure `pyproject.toml` `pythonpath = ["src"]` already exposes the package; no changes needed.
- **Files touched**: `src/nora/intervention_memory/__init__.py` (new, 1-line docstring re-stating the read-only hard rule), `tests/intervention_memory/__init__.py` (new, empty), `tests/intervention_memory/fixtures/.gitkeep` (new).
- **Spec scenarios covered**: prerequisite for R1-R11.
- **Depends on**: none.
- **Estimated LOC**: 5 production, 2 test.
- **Verification**: `python -c "import nora.intervention_memory"` exits 0; `ls src/nora/intervention_memory/ tests/intervention_memory/` shows both directories.

## Task 6.1 — ✅ DONE — AST no-write detector + `test_no_writes.py` (R2)

- **Goal**: First committed thing in the package is the AST test that catches any future write call.
- **Strict TDD**: RED — write `tests/intervention_memory/test_no_writes.py::test_no_writable_file_calls_under_intervention_memory` asserting the offenders list is `[]`. GREEN — implement `_iter_python_files()` over `src/nora/intervention_memory/` + AST walker that flags `ast.Call` whose func attribute is `write_text|write_bytes|unlink|os.replace|os.remove|shutil.rmtree|os.removedirs|os.makedirs` plus `open(...)` with mode ∈ `{w,a,x,+}`. REFACTOR — add regex fallback for AST-edge cases (`getattr(path, "write_text")(...)`) and a parameterised mode helper.
- **Files touched**: `tests/intervention_memory/test_no_writes.py` (new), mirror `tests/test_driver_airgap.py` AST pattern.
- **Spec scenarios covered**: R2-S1 (zero writable calls), R2-S3 (`open(..., "r")` allowed).
- **Depends on**: Task 0.1.
- **Estimated LOC**: 0 production, 95 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_no_writes.py::test_no_writable_file_calls_under_intervention_memory -v` exits 0 against the empty package.

## Task 6.2 — ✅ DONE — AST no-pytest-import detector (R11)

- **Goal**: Add the second scan in `test_no_writes.py`: zero `pytest`/`monkeypatch`/`_pytest` imports in any production module under `src/nora/intervention_memory/`.
- **Strict TDD**: RED — `test_production_modules_do_not_import_pytest` asserts zero matches. GREEN — `_find_banned_imports()` helper using the same banned-list shape as `tests/test_driver_airgap.py::test_no_banned_imports_in_driver_layer`. REFACTOR — share the helper with Task 6.3's banned-import list.
- **Files touched**: `tests/intervention_memory/test_no_writes.py` (modify).
- **Spec scenarios covered**: R11-S2.
- **Depends on**: Task 6.1.
- **Estimated LOC**: 0 production, 25 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_no_writes.py::test_production_modules_do_not_import_pytest -v` exits 0.

## Task 6.3 — ✅ DONE — AST one-way dependency scan (R-NEW-4)

- **Goal**: Extend the banned-import scan to forbid production modules from importing `nora.server` / `nora.drivers` / `nora.intervention_memory.shim_webui` (the one-way dependency rule).
- **Strict TDD**: RED — two tests: `test_production_modules_do_not_import_shim_webui` and `test_production_modules_do_not_import_nora_server`. GREEN — extend `_find_banned_imports` with the new banned list. REFACTOR — single helper iterates over a `_BANNED_IMPORTS: tuple[str, ...]` constant for clarity.
- **Files touched**: `tests/intervention_memory/test_no_writes.py` (modify).
- **Spec scenarios covered**: R10-S3, R-NEW-4-S2, R-NEW-4-S3.
- **Depends on**: Task 6.2.
- **Estimated LOC**: 0 production, 35 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_no_writes.py -v` exits 0 with all three no-import tests passing.

## Task 6.4 — ✅ DONE — AST guard self-test poison (R2-S2)

- **Goal**: Prove the AST detector would catch a developer who adds a real `Path.write_text` to `storage.py`.
- **Strict TDD**: RED — write `test_injected_write_text_call_fails_build` that copies `src/nora/intervention_memory/` to `tmp_path`, monkey-patches the copy with `Path("/tmp/x").write_text("x")`, runs the detector against the copy, asserts the offenders list is non-empty with `("storage.py", ..., "write_text")`. GREEN — implement the copy+monkeypatch dance. REFACTOR — assert the error message format matches `(relative_path, lineno, call_name)` so future regression output is grep-able.
- **Files touched**: `tests/intervention_memory/test_no_writes.py` (modify).
- **Spec scenarios covered**: R2-S2, R-NEW-3-S1.
- **Depends on**: Task 6.3.
- **Estimated LOC**: 0 production, 30 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_no_writes.py::test_injected_write_text_call_fails_build -v` exits 0 (poison is detected, test asserts detection).

## Task 1.1 — ✅ DONE — Pydantic models (R1)

- **Goal**: Define `InterventionMemoryRecord`, `NetworkEquipmentBlock`, `PreExistingOfflineSubscriber` with `extra="ignore"`, `Literal` enums for `stage`/`status`, every non-required field `Optional[T] = None`, and the v7/v8 forward/backward tolerance.
- **Strict TDD**: RED — three tests in `tests/intervention_memory/test_models.py`: `test_v7_record_parses_without_recommended_action`, `test_v8_record_with_mac_address_alias_is_tolerated`, `test_invalid_stage_literal_raises_validation_error`. GREEN — write the three BaseModel classes with `model_config = ConfigDict(extra="ignore")`, `stage: Literal["PRE_DIAGNOSTIC", ...]`, canonical `mac: Optional[str] = None`. REFACTOR — group Literal enums into module-level tuples, add `model_config` per-class consistently.
- **Files touched**: `src/nora/intervention_memory/models.py` (new), `tests/intervention_memory/test_models.py` (new).
- **Spec scenarios covered**: R1-S1, R1-S2, R1-S3.
- **Depends on**: Task 6.4.
- **Estimated LOC**: 95 production, 110 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_models.py -v` exits 0 with all 3 RED tests GREEN.

## Task 1.2 — ✅ DONE — Fixture records (v7 + v8 + invalid)

- **Goal**: Create JSON fixtures the storage + sanitize + tools tests will share.
- **Strict TDD**: RED — implicit (storage tests in Task 2.1 cannot run without fixtures). GREEN — write `tests/intervention_memory/fixtures/v7_baseline.json` (no `recommended_action`, no `hardware_band`, `mac` not `mac_address`), `v8_pre_migration.json` (carries `recommended_action`, `hardware_band`, one subscriber with `mac_address` alias), `validation_failure.json` (parses as JSON but fails `InterventionMemoryRecord`). REFACTOR — use RFC 5737 IPs (`192.0.2.x`) per design §10 zero-leakage rule; no real production data.
- **Files touched**: `tests/intervention_memory/fixtures/v7_baseline.json`, `v8_pre_migration.json`, `validation_failure.json` (all new).
- **Spec scenarios covered**: fixture for R1, R3, R4, R5, R6, R9.
- **Depends on**: Task 1.1 (schema must exist to validate).
- **Estimated LOC**: 0 production, 88 test.
- **Verification**: `uv run python -c "from tests.intervention_memory.fixtures import _load_v7; print(_load_v7())"` succeeds (or `cat fixtures/v7_baseline.json | uv run python -m json.tool`).

## Task 2.1 — ✅ DONE — Storage `read_records(settings)` with tolerant parser (R3)

- **Goal**: Implement the read-only glob + tolerant per-file parser that returns `list[InterventionMemoryRecord]`.
- **Strict TDD**: RED — three tests in `tests/intervention_memory/test_storage.py`: `test_corrupt_json_file_is_skipped` (3 valid + 1 `}.invalid.json` returning 3), `test_validation_error_is_skipped` (1 valid + 1 `validation_failure.json` returning 1), `test_missing_directory_returns_empty_list`. GREEN — minimal `read_records(settings)` using `Path(settings.nora_interventions_dir).glob("*.json")` + `try: json.load + InterventionMemoryRecord.model_validate except (json.JSONDecodeError, ValidationError): logger.warning(...)`. REFACTOR — extract `_load_one(path) -> InterventionMemoryRecord | None`, add a `_is_existing_dir` guard so missing dir returns `[]` instead of `FileNotFoundError`.
- **Files touched**: `src/nora/intervention_memory/storage.py` (new), `tests/intervention_memory/test_storage.py` (new).
- **Spec scenarios covered**: R3-S1, R3-S2, R3-S3.
- **Depends on**: Task 1.1, Task 1.2.
- **Estimated LOC**: 50 production, 95 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_storage.py -v` exits 0 with 3 GREEN tests.

## Task 2.2 — ✅ DONE — Storage WARNING log + missing-dir no-mkdir contract

- **Goal**: Assert WARNING log lines are emitted on per-file errors and the missing-dir path does NOT create the directory.
- **Strict TDD**: RED — `test_corrupt_json_emits_warning_with_filename` (uses `caplog`), `test_missing_directory_does_not_create_path` (asserts the path still doesn't exist after the call). GREEN — add `caplog.at_level("WARNING")` context + `assert not path.exists()` checks. REFACTOR — move log message format to a constant so future log scrapers have a stable contract.
- **Files touched**: `tests/intervention_memory/test_storage.py` (modify).
- **Spec scenarios covered**: R3-S1 (warning content), R3-S3 (no mkdir).
- **Depends on**: Task 2.1.
- **Estimated LOC**: 0 production, 25 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_storage.py -v -k "warning or missing"` exits 0.

## Task 3.1 — ✅ DONE — Sanitize recursive walker + `_BYPASS_FIELDS` (R9)

- **Goal**: Build the recursive `_sanitize_dict` walker with the bypass list.
- **Strict TDD**: RED — `test_structured_top_level_fields_bypass_sanitizer` (asserts `intervention_id` byte-identical even when it contains `10.0.0.5` substring). GREEN — `_BYPASS_FIELDS = frozenset({"target_ip","intervention_id","stage","status","timestamp_unix","timestamp_iso","created_at","ticket_number"})` + `_sanitize_dict(value, sanitizer)` recursion (dict → check bypass → else recurse; list → recurse on items; str → `sanitizer.sanitize(s).text`). REFACTOR — add type hints (`dict[str, Any]`) and module docstring citing `tests/test_sanitizer.py` as the upstream contract.
- **Files touched**: `src/nora/intervention_memory/sanitize.py` (new), `tests/intervention_memory/test_sanitize.py` (new).
- **Spec scenarios covered**: R9-S3, R-NEW-2-S2.
- **Depends on**: Task 1.1.
- **Estimated LOC**: 45 production, 50 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_sanitize.py::test_structured_top_level_fields_bypass_sanitizer -v` exits 0.

## Task 3.2 — ✅ DONE — Sanitize public API `sanitize_record_payload` (R9)

- **Goal**: Public entry point that walks a full record (including nested `network_equipment.pre_existing_offline_subscribers`) through the sanitizer.
- **Strict TDD**: RED — `test_ipv4_in_record_name_is_masked` (private IPv4 in `record_name` → `RADIO_NODE_*` alias), `test_mac_in_subscriber_note_is_masked` (MAC in `note` → `SWITCH_ACC_*`), `test_free_text_fields_sanitized_in_search_output` (record_name containing IP → alias appears in `record_name` key). GREEN — `sanitize_record_payload(record, sanitizer) -> dict` calls `record.model_dump(mode="json")` then `_sanitize_dict`. REFACTOR — narrow return type to `dict[str, Any]` and import `Sanitizer` from `nora.sanitizer`.
- **Files touched**: `src/nora/intervention_memory/sanitize.py` (modify), `tests/intervention_memory/test_sanitize.py` (modify).
- **Spec scenarios covered**: R4-S5, R6-S4, R9-S1, R9-S2.
- **Depends on**: Task 3.1.
- **Estimated LOC**: 30 production, 40 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_sanitize.py -v` exits 0 with all 4 RED tests GREEN.

## Task 4.1 — ✅ DONE — Correlation pure functions (R6)

- **Goal**: `match_tower(system_name, tower_name)` substring `lower()` match + `classify_conflict(carrier, target, width)` CO/ADJACENT/CLEAR classification.
- **Strict TDD**: RED — three tests in `tests/intervention_memory/test_correlation.py`: `test_match_tower_substring_case_insensitive`, `test_classify_conflict_equal_carrier_returns_co_channel`, `test_classify_conflict_nearby_carrier_returns_adjacent`, `test_classify_conflict_clear_when_delta_exceeds_width`. GREEN — 15 LOC: `def match_tower(...): return tower_name.lower() in system_name.lower()`; `def classify_conflict(...): delta = abs(carrier - target); if delta < 0.5: return "CO_CHANNEL"; if delta < width: return "ADJACENT_CHANNEL"; return "CLEAR"`. REFACTOR — module docstring documenting the `tower_name="A"` substring false-match caveat (out of scope to fix).
- **Files touched**: `src/nora/intervention_memory/correlation.py` (new), `tests/intervention_memory/test_correlation.py` (new).
- **Spec scenarios covered**: R6-S1, R6-S2, R6-S3.
- **Depends on**: Task 0.1 (can land in parallel with Group 2-3; pure functions, no deps).
- **Estimated LOC**: 60 production, 75 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_correlation.py -v` exits 0.

## Task 5.1 — ✅ DONE — `search_intervention_history` library function (R4, R7)

- **Goal**: The source-of-truth tool body: read + filter (`target_ip`, `ticket_number`, `stage`, `keyword`) + sort DESC + slice `[:limit]` + sanitize + cap keyword I/O at `Settings.nora_interventions_keyword_search_max_records`.
- **Strict TDD**: RED — six tests in `tests/intervention_memory/test_tools.py`: `test_search_target_ip_exact_match`, `test_search_keyword_substring_match_uses_json_dumps_lowering`, `test_results_sorted_by_timestamp_unix_descending`, `test_limit_clamps_result_set`, `test_keyword_search_cap_enforced_and_warning_logged` (1500 files, cap=1000, monkeypatch counter), `test_keyword_search_under_cap_no_warning`. GREEN — minimal `def search_intervention_history(settings, target_ip=None, ticket_number=None, stage=None, keyword=None, limit=5)` that calls `storage.read_records`, applies filters, sorts, slices, then maps `sanitize_record_payload` over each record. REFACTOR — extract `_filter_record(r, target_ip, ticket_number, stage, keyword)` helper and a `_enforce_keyword_cap(...)` closure that logs the WARNING when truncated.
- **Files touched**: `src/nora/intervention_memory/tools.py` (new, partial), `tests/intervention_memory/test_tools.py` (new, partial).
- **Spec scenarios covered**: R4-S1..S5, R7-S1, R7-S2.
- **Depends on**: Task 2.2, Task 3.2.
- **Estimated LOC**: 65 production, 70 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_tools.py -v -k "search or keyword"` exits 0 with 6 GREEN tests.

## Task 5.2 — ✅ DONE — `get_device_lifecycle_summary` library function (R5)

- **Goal**: Aggregated lifecycle audit: returns `NO_HISTORY_FOUND` on empty, otherwise 6-field SUCCESS dict with offline subscribers from the most-recent PRE_DIAGNOSTIC.
- **Strict TDD**: RED — three tests: `test_lifecycle_summary_no_history_returns_status_marker` (empty tmp dir → exact dict match), `test_lifecycle_summary_success_returns_all_six_fields` (2 records, assert all 6 keys + correct ordering), `test_known_pre_existing_offline_subscribers_from_latest_pre_diagnostic` (2 PRE_DIAGNOSTIC records → newer wins). GREEN — minimal body: `summary = search_intervention_history(settings, target_ip=target_ip, limit=20)`; if empty → `NO_HISTORY_FOUND`; else build the dict with `records[0]` as `latest_intervention` (sanitized) and `_pick_offline_subscribers(records)`. REFACTOR — extract `_pick_offline_subscribers(records: list[InterventionMemoryRecord]) -> list[dict]` that filters by `stage == "PRE_DIAGNOSTIC"` and takes the first (most-recent due to sort).
- **Files touched**: `src/nora/intervention_memory/tools.py` (modify), `tests/intervention_memory/test_tools.py` (modify).
- **Spec scenarios covered**: R5-S1, R5-S2, R5-S3.
- **Depends on**: Task 5.1.
- **Estimated LOC**: 35 production, 35 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_tools.py -v -k "lifecycle or summary"` exits 0 with 3 GREEN tests.

## Task 5.3 — ✅ DONE — `correlate_sector_interference` library function (R6)

- **Goal**: Walk the most-recent `nora_interventions_correlate_scan_limit` records, apply `match_tower` + frequency match, classify CO vs ADJACENT, return `{tower_name, proposed_frequency_mhz, channel_width_mhz, is_frequency_clear_on_tower, detected_conflicts[]}`.
- **Strict TDD**: RED — four tests: `test_correlate_equal_carrier_returns_co_channel` (`delta == 0.0`), `test_correlate_nearby_carrier_returns_adjacent_channel` (delta < width), `test_correlate_tower_mismatch_returns_zero_conflicts` (`detected_conflicts == []`, `is_frequency_clear_on_tower is True`), `test_correlate_result_neighbor_sanitized` (`neighbor_device` contains `RADIO_NODE_*`, no private IP). GREEN — minimal body: cap at `scan_limit`, for each record with `network_equipment.system_name AND carrier_frequency_mhz is not None` apply `match_tower` + `abs(carrier - target) < width`, classify, append to `detected_conflicts`. REFACTOR — extract `_build_conflict_entry(record, target_frequency_mhz)` and sort conflicts by `frequency_delta_mhz` ASC for deterministic output.
- **Files touched**: `src/nora/intervention_memory/tools.py` (modify), `tests/intervention_memory/test_tools.py` (modify), `tests/intervention_memory/test_sanitize.py` (add `test_correlate_result_neighbor_sanitized`).
- **Spec scenarios covered**: R6-S1..S4.
- **Depends on**: Task 4.1, Task 5.1.
- **Estimated LOC**: 30 production, 40 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_tools.py tests/intervention_memory/test_sanitize.py -v -k correlate` exits 0 with 4 GREEN tests.

## Task 7.1 — ✅ DONE — `shim_webui.py` with `class Tools` (R10)

- **Goal**: webui.db mirror shim with three `async def` methods (`search_intervention_history`, `get_device_lifecycle_summary`, `correlate_sector_interference`) each accepting and ignoring `__event_emitter__`, plus three class constants (`INTERVENTIONS_DIR`, `KEYWORD_SEARCH_MAX_RECORDS`, `CORRELATE_SCAN_LIMIT`) for the openchat deploy script to overwrite.
- **Strict TDD**: RED — `test_shim_tools_class_exposes_three_async_methods` (uses `inspect.getsource(Tools)` and greps for the three `async def` lines). GREEN — minimal `class Tools` with three `async def` methods that delegate 1:1 to `nora.intervention_memory.tools` (the source of truth). REFACTOR — share a private helper `_settings_from_env_or_singleton()` so each method stays a 3-line body; update `__init__.py` to re-export `Tools`.
- **Files touched**: `src/nora/intervention_memory/shim_webui.py` (new), `src/nora/intervention_memory/__init__.py` (modify), `tests/intervention_memory/test_tools.py` (add the inspect test).
- **Spec scenarios covered**: R10-S1.
- **Depends on**: Task 5.1 (search must exist for delegation).
- **Estimated LOC**: 70 production, 20 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_tools.py::test_shim_tools_class_exposes_three_async_methods -v` exits 0.

## Task 7.2 — ✅ DONE — Shim delegation no-drift (R10)

- **Goal**: Assert the shim's methods call the library functions exactly once with the same kwargs, so MCP and webui.db cannot drift.
- **Strict TDD**: RED — `test_shim_methods_delegate_to_tools_module` (monkeypatch `nora.intervention_memory.tools.search_intervention_history` to return sentinel `{"delegated": True}`; `await Tools().search_intervention_history(target_ip="10.0.0.5")`; assert patched fn called once with `target_ip="10.0.0.5"` and result equals sentinel). GREEN — write the test against the shim. REFACTOR — parameterise the test across all three methods to lock in delegation for all of them.
- **Files touched**: `tests/intervention_memory/test_tools.py` (modify).
- **Spec scenarios covered**: R10-S2.
- **Depends on**: Task 7.1.
- **Estimated LOC**: 0 production, 25 test.
- **Verification**: `uv run pytest tests/intervention_memory/test_tools.py -v -k shim` exits 0 with all 3 method delegations GREEN.

## Task 8.1 — ✅ DONE — `Settings` fields for interventions dir + caps (R8)

- **Goal**: Extend `src/nora/config.py` with three env-overridable fields: `nora_interventions_dir: Path` (env `NORA_INTERVENTIONS_DIR`, default `./var/interventions/`), `nora_interventions_keyword_search_max_records: int` (default 1000), `nora_interventions_correlate_scan_limit: int` (default 50).
- **Strict TDD**: RED — two tests in `tests/test_config.py`: `test_env_var_overrides_default_interventions_dir` (`monkeypatch.setenv("NORA_INTERVENTIONS_DIR", "/tmp/custom/")`), `test_defaults_apply_with_no_env_override` (no env → defaults match spec). GREEN — add the three fields to the existing `Settings` model in `src/nora/config.py` (next to the `nora_session_journal_*` block per design §2). REFACTOR — add a `# --- Intervention memory MCP ---` section comment so future settings additions stay grouped.
- **Files touched**: `src/nora/config.py` (modify, +9 LOC), `tests/test_config.py` (modify, +30 LOC test).
- **Spec scenarios covered**: R8-S1, R8-S2.
- **Depends on**: none (config is independent of the package).
- **Estimated LOC**: 9 production, 30 test.
- **Verification**: `uv run pytest tests/test_config.py -v -k intervention` exits 0 with both env override + default tests GREEN.

## Task 8.2 — ✅ DONE — `.env.example` adds 3 keys with comments (R8)

- **Goal**: Document the three env vars in `.env.example` so operators can wire the production path on `.22` without committing real infrastructure values.
- **Strict TDD**: RED — `test_env_example_documents_three_intervention_memory_keys` (read `.env.example`, assert all three `NORA_INTERVENTIONS_*` keys appear with `# Intervention memory MCP` section comment + sanitized placeholders). GREEN — append the section to `.env.example` with `NORA_INTERVENTIONS_DIR=./var/interventions/` (relative default) + `NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS=1000` + `NORA_INTERVENTIONS_CORRELATE_SCAN_LIMIT=50` (cap keys commented-out as overrides). REFACTOR — add a comment line `## Intervention memory MCP — operator wires the real .22 path here` for documentation.
- **Files touched**: `.env.example` (modify, +12 LOC), `tests/test_config.py` (modify, +20 LOC test).
- **Spec scenarios covered**: R8 hygiene (no real production path committed).
- **Depends on**: Task 8.1.
- **Estimated LOC**: 12 production, 20 test.
- **Verification**: `grep -E "^#?(NORA_INTERVENTIONS_)" .env.example` shows all three keys; `uv run pytest tests/test_config.py -v -k env_example` exits 0.

## Task 9.1 — ✅ DONE — Register 3 `@mcp.tool` in `src/nora/server.py` (R-NEW-1)

- **Goal**: Wire the three tool wrappers on the global `mcp = FastMCP("nora")` instance, re-export the names in `__all__`.
- **Strict TDD**: RED — `test_server_module_exports_three_new_tool_names` (asserts the three names appear in `nora.server.__all__` and that `from nora.server import search_intervention_history, get_device_lifecycle_summary, correlate_sector_interference` succeeds). GREEN — add the import line + three `@mcp.tool` wrappers (3-line bodies that call `get_runtime_state()` then delegate to the library function) + append the three names to `__all__`. REFACTOR — docstrings on each tool quote spec §5.1/5.2/5.3 + mention the read-only guarantee + the sanitizer boundary.
- **Files touched**: `src/nora/server.py` (modify, +28 LOC), `tests/test_server.py` (modify, +15 LOC test).
- **Spec scenarios covered**: R-NEW-1-S1.
- **Depends on**: Task 5.3 (all 3 library functions must exist), Task 8.1 (Settings fields for `get_runtime_state()` to thread).
- **Estimated LOC**: 28 production, 15 test.
- **Verification**: `uv run pytest tests/test_server.py -v -k "intervention_memory or three_new_tools"` exits 0.

## Task 9.2 — ✅ DONE — MCP wrapper delegation (R-NEW-1-S2)

- **Goal**: Prove the `@mcp.tool` wrappers call the library functions with the same kwargs.
- **Strict TDD**: RED — `test_mcp_tool_wrapper_delegates_to_pure_library_function` (monkeypatch `nora.intervention_memory.tools.search_intervention_history`; `set_runtime_state(settings=fake, provider=fake)`; invoke the registered MCP tool via `mcp._tool_manager._tools["search_intervention_history"].fn(target_ip="10.0.0.5")` or `await mcp.call_tool(...)`; assert patched fn called once with `target_ip="10.0.0.5"`). GREEN — write the test. REFACTOR — use `pytest-httpserver`-style mocking pattern from existing `tests/test_server.py` so the test is hermetic.
- **Files touched**: `tests/test_server.py` (modify, +25 LOC test).
- **Spec scenarios covered**: R-NEW-1-S2.
- **Depends on**: Task 9.1.
- **Estimated LOC**: 0 production, 25 test.
- **Verification**: `uv run pytest tests/test_server.py -v -k "wrapper delegates"` exits 0.

## Task 9.3 — ✅ DONE — Auto-trace middleware records new tool call (R-NEW-1-S3)

- **Goal**: Prove `_AutoTraceMiddleware` records every invocation of the three new tools as a `SessionStep` with `outcome == "success"` (no middleware change required — just verification).
- **Strict TDD**: RED — `test_auto_trace_middleware_records_search_intervention_history_call` (run `register_auto_trace_middleware(journal)`; invoke the registered MCP tool; assert journal has one `SessionStep` with `tool == "search_intervention_history"` and `outcome == "success"`). GREEN — write the test against the existing `_AutoTraceMiddleware` shape from `src/nora/server.py:255-309`. REFACTOR — parameterise across all three new tool names.
- **Files touched**: `tests/test_server_auto_trace.py` (modify, +30 LOC test).
- **Spec scenarios covered**: R-NEW-1-S3.
- **Depends on**: Task 9.1.
- **Estimated LOC**: 0 production, 30 test.
- **Verification**: `uv run pytest tests/test_server_auto_trace.py -v -k "intervention"` exits 0 with 3 GREEN tests (one per tool).

## Task 9.4 — ✅ DONE — Sanitizer bound at MCP tool boundary (R-NEW-2)

- **Goal**: End-to-end verification that free-text fields in the tool output are sanitized AND structured top-level fields bypass.
- **Strict TDD**: RED — two tests in `tests/test_server.py` or `tests/intervention_memory/test_tools.py`: `test_free_text_fields_in_search_output_sanitized_via_mcp_wrapper` (record with `record_name` containing `10.53.12.4` → MCP tool output's `record_name` does NOT contain `10.53.12.4`, contains `RADIO_NODE_*`), `test_structured_top_level_fields_bypass_via_mcp_wrapper` (`intervention_id == "INT-1-10.0.0.5-..."` is byte-identical in the output). GREEN — write the tests against the registered MCP tool wrappers, using a real `Sanitizer()` instance via `set_runtime_state`. REFACTOR — share a `seed_intervention_record(...)` helper across both tests.
- **Files touched**: `tests/test_server.py` (modify, +35 LOC test).
- **Spec scenarios covered**: R-NEW-2-S1, R-NEW-2-S2.
- **Depends on**: Task 9.1, Task 3.2.
- **Estimated LOC**: 0 production, 35 test.
- **Verification**: `uv run pytest tests/test_server.py -v -k "sanitiz and intervention"` exits 0.

## Task 10.1 — ✅ DONE — Coverage ≥ 85% gate on `intervention_memory` package (R11-S1)

- **Goal**: Prove the new package meets the project's 85% coverage floor.
- **Strict TDD**: RED — run `uv run pytest --cov=src/nora/intervention_memory --cov-fail-under=85`. GREEN — if it fails, add the missing tests as part of this task (likely `test_correlate_sector_interference` empty-window + `storage.read_records` empty-dir returns `[]` boundary cases). REFACTOR — adjust the threshold to 88% per design §11 R11 forecast once GREEN.
- **Files touched**: any test file in `tests/intervention_memory/` that needs the gap closed (likely none if all previous tasks landed with full coverage).
- **Spec scenarios covered**: R11-S1.
- **Depends on**: all previous tasks.
- **Estimated LOC**: 0 production, ≤20 test (top-up only if needed).
- **Verification**: `uv run pytest --cov=src/nora/intervention_memory --cov-fail-under=85 tests/intervention_memory/` exits 0; reported coverage ≥ 88%.

## Task 10.2 — ✅ DONE — `mypy --strict` + `ruff check` clean

- **Goal**: Verify the new package + modified files pass `uv run mypy --strict src/nora` and `uv run ruff check .` and `uv run ruff format --check .`.
- **Strict TDD**: RED — run the three commands, observe failures. GREEN — fix any reported errors (`# type: ignore[import-untyped]` for `pydantic_settings` import if needed; `# noqa: E501` only when justified; format with `uv run ruff format`). REFACTOR — no refactor; this is a gate, not a feature.
- **Files touched**: any of `src/nora/intervention_memory/*.py`, `src/nora/server.py`, `src/nora/config.py` if a fix is needed.
- **Spec scenarios covered**: design §11 quality gates.
- **Depends on**: all previous tasks.
- **Estimated LOC**: 0–10 (fix-up).
- **Verification**: `uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src/nora` all exit 0.

## Task 10.3 — ✅ DONE — End-to-end MCP smoke (boot + 3 tools visible)

- **Goal**: Prove the global `FastMCP("nora")` instance exposes the three new tools and the existing 6 still work after wiring.
- **Strict TDD**: RED — `test_mcp_instance_exposes_all_nine_tools` (import `nora.server`, assert `mcp._tool_manager._tools` contains 9 names: 6 existing + 3 new). GREEN — write the test. REFACTOR — sort the names to make failure messages stable.
- **Files touched**: `tests/test_server.py` (modify, +25 LOC test).
- **Spec scenarios covered**: integration sanity check (no spec scenario, but required for the design §11 "nora_health still works" acceptance criterion #4).
- **Depends on**: Task 9.1.
- **Estimated LOC**: 0 production, 25 test.
- **Verification**: `uv run pytest tests/test_server.py tests/test_server_auto_trace.py tests/test_server_session_tools.py tests/test_server_driver_tool.py -v` exits 0; `nora.server.__all__` lists 9 tool names.

---

## Implementation Order

1. **Scaffold + AST guard (Tasks 0.1, 6.1–6.4)** — empty package + the AST detector + banned-import scanner + poison self-test. The AST guard is the FIRST committed thing in the new package so any future write call is caught at PR time.
2. **Models + fixtures (Tasks 1.1, 1.2)** — Pydantic schema + JSON fixtures.
3. **Storage + sanitize + correlation (Tasks 2.1, 2.2, 3.1, 3.2, 4.1)** — pure logic; Tasks 4.1 (correlation) can land in parallel with 2.x and 3.x because it has no internal deps.
4. **Tools library (Tasks 5.1, 5.2, 5.3)** — the three source-of-truth tool bodies; the shim (7.x) and the MCP wrappers (9.x) both delegate to these.
5. **Shim (Tasks 7.1, 7.2)** — webui.db mirror shim delegates to Task 5.x.
6. **Config + .env (Tasks 8.1, 8.2)** — independent of package; can land in parallel with Groups 1–7.
7. **Server wiring (Tasks 9.1–9.4)** — `@mcp.tool` registrations + delegation tests + auto-trace verification + sanitizer boundary at the MCP edge.
8. **Coverage + lint + smoke (Tasks 10.1–10.3)** — quality gates.

## Suggested PR Split (matches `Review Workload Forecast`)

- **PR 1 — Pure logic + tests + shim (Tasks 0.1, 6.1–6.4, 1.1–1.2, 2.1–2.2, 3.1–3.2, 4.1, 5.1–5.3, 7.1–7.2)** — `src/nora/intervention_memory/` (7 production files, ~565 LOC) + `tests/intervention_memory/` (6 test files + 3 fixtures, ~688 LOC). Total ≈ 1253 LOC. **Review focus**: schema tolerance + sanitization boundary + read-only guarantee (AST guard). Self-contained: nothing in `src/nora/` outside the new package changes.
- **PR 2 — NORA wiring + config + integration gate (Tasks 8.1–8.2, 9.1–9.4, 10.1–10.3)** — `src/nora/server.py` (+28) + `src/nora/config.py` (+9) + `.env.example` (+12) + config/wiring tests (~205 LOC test additions across `tests/test_config.py`, `tests/test_server.py`, `tests/test_server_auto_trace.py`). Total ≈ 49 LOC production + 205 LOC tests. **Review focus**: pure-function delegation + Settings threading + `.env.example` hygiene.

## Notes / Invariants

- **AST guard lands first**: `tests/intervention_memory/test_no_writes.py` is the first committed file in the new package directory. Per `tests/test_driver_airgap.py` + `tests/test_session_journal_airgap.py` precedent. RED test runs against the empty scaffold and passes; subsequent production code commits keep it green.
- **Self-test poison needs tmp_copy**: Task 6.4 cannot inject a write into the real package and still have the package function correctly — it must copy `src/nora/intervention_memory/` to `tmp_path`, monkeypatch the copy, run the detector against the copy, then assert the offenders list is non-empty. The copy is discarded at test teardown.
- **Shim does NOT import from `nora.server`**: Task 6.3 enforces this. The shim imports from `nora.intervention_memory.tools` only — one-way dependency.
- **Settings threading via `get_runtime_state()`**: each `@mcp.tool` wrapper reads `settings, _ = get_runtime_state()` and passes `settings` to the library function. The library functions NEVER touch `os.environ` — all config flows through Pydantic Settings.
- **Test command prefix**: every task verification is `uv run pytest ...` per `openspec/config.yaml:74` (strict-markers) and the orchestrator's preflight (strict-config). No `pytest-asyncio` introduced; shim `async def` methods are tested via delegation (Task 7.2) without `await` because the shim bodies delegate synchronously to library functions.
- **Word budget**: this artifact exceeds the SKILL.md 530-word default deliberately because each task carries 8 structured properties (Goal / Strict TDD / Files / Scenarios / Deps / LOC / Verification) per the orchestrator's preflight. The per-task detail is the test contract for the apply phase.
