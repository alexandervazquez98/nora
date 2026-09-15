# Tasks: 2026-09-15-register-device-mcp

**Change**: 2026-09-15-register-device-mcp
**Issue**: closes #42
**Capability**: ad-hoc-device-registration (NEW); `nora-mcp-server`, `driver-snmp-pmp450i`, `prompts` (MODIFIED)
**Strategy**: single-pr (per preflight)
**Review budget**: 800 lines (per preflight; orchestrator chose single-pr over chained)
**Strict TDD**: every impl task writes the test first, runs it RED, then GREENs

---

## 1. Add exception types in `src/nora/drivers/exceptions.py`

**Spec scenarios covered**: Inv-1-S2 (duplicate-id), Tool-S2 (unreachable), Tool-S4 (malformed host), Tool-S5 (invalid community)
**Design ref**: §4.2

**Files touched**:
- modified: `src/nora/drivers/exceptions.py` (+~25 lines; append four `DriverError` subclasses)

**Strict TDD sequence**:
1. Test: `tests/test_driver_exceptions.py::test_device_unreachable_is_driver_error_subclass` — `DeviceUnreachable("192.0.2.10")` is a `DriverError`; carries the host string.
2. Test: `tests/test_driver_exceptions.py::test_invalid_community_is_driver_error_subclass` — same shape for `InvalidCommunity`.
3. Test: `tests/test_driver_exceptions.py::test_invalid_host_error_is_driver_error_subclass` — same shape for `InvalidHostError`.
4. Test: `tests/test_driver_exceptions.py::test_duplicate_device_error_is_driver_error_subclass` — `DuplicateDeviceError("ap-7400-01")` is a `DriverError`.
5. Implementation: append the four classes to `exceptions.py` (no new imports).
6. Verify: `uv run pytest tests/test_driver_exceptions.py -v` → 4 new tests green; full `test_driver_exceptions.py` still green.

**Work-unit boundary**: single commit `feat(drivers): typed errors for register_device`. Tests live with the production types they assert against.
**LoC estimate**: +25 production, +30 tests.
**Dependencies**: none.

---

## 2. Implement `MutableInventory` wrapper + `_InventoryLike` Protocol

**Spec scenarios covered**: Inv-1-S1, Inv-1-S2, Inv-1-S3, Inv-1-S4, Inv-1-S5
**Design ref**: §4.1, §6

**Files touched**:
- new: `src/nora/drivers/mutable_inventory.py` (~85 lines)
- new: `tests/test_mutable_inventory.py` (~110 lines, 5 scenarios)

**Strict TDD sequence**:
1. Test: `tests/test_mutable_inventory.py::test_register_inserts_new_device` — `MutableInventory(seed=Inventory.from_yaml(empty_yaml))`; `wrapper.register(Device(...))` then `wrapper.get(id)` returns it.
3. Test: `test_register_rejects_duplicate_device_id` — `wrapper.register` twice → `DuplicateDeviceError("ap-7400-01")`; original entry preserved.
4. Test: `test_unregister_removes_runtime_device` — `register` then `unregister(id)` → `wrapper.get(id)` raises `DeviceNotFoundError`.
5. Test: `test_unregister_unknown_id_raises_device_not_found` — `unregister("nonexistent")` raises `DeviceNotFoundError`.
6. Test: `test_get_delegates_to_base_inventory_for_yaml_loaded_entry` — `seed.from_yaml(...)` carries `"ap-7400-01"`; `wrapper.get("ap-7400-01")` returns the YAML-loaded entry without mutating the frozen `Inventory`.
7. Implementation: `mutable_inventory.py` with `_InventoryLike` Protocol + `MutableInventory` (overlay dict + threading.Lock on register/unregister; reads lock-free).
8. Verify: `uv run pytest tests/test_mutable_inventory.py -v` → 5 tests green.

**Work-unit boundary**: single commit `feat(drivers): MutableInventory wrapper preserving frozen Inventory`. Pure foundation — does not touch the driver yet, so the existing 217-line `test_inventory.py` back-compat suite stays green (only `Inventory` is exposed there).
**LoC estimate**: +85 production, +110 tests.
**Dependencies**: Task 1 (uses `DuplicateDeviceError`).

---

## 3. Migrate 8 `Inventory.get()` call sites to route through `MutableInventory`

**Spec scenarios covered**: Tool-S7 (wrapper-routing regression)
**Design ref**: §6 (table of 8 sites); §3 driver.py:80 ctor

**Files touched**:
- modified: `src/nora/drivers/snmp_pmp450i/driver.py` (~10 lines: ctor signature `Inventory` → `_InventoryLike`; both already satisfy Protocol so back-compat preserved)
- modified: `src/nora/cli.py` (~5 lines: wrap `Inventory.from_yaml(...)` into `MutableInventory(...)` at lines 237-238)
- new (test pin): `tests/test_register_device.py::test_register_device_routes_through_wrapper` (lives in step 4's file but is written now)

**Strict TDD sequence**:
1. Test: `tests/test_register_device.py::test_register_device_routes_through_wrapper` — inject `MutableInventory`; call `register_device(host=...)` with fake sysDescr; then call `snmp_get_ap_summary(device_id=<returned>)` succeeds against the overlay (asserts no bypass).
2. Implementation: switch `cli.py:237-238` to `MutableInventory(base=Inventory.from_yaml(...))`; broaden `Pmp450iDriver.__init__` ctor type hint to `_InventoryLike`. 8 call sites in §6 already use `self._inventory.get(...)`; the wrapper's `get` delegates correctly, so no call-site edits.
3. Verify: `uv run pytest tests/test_driver_snmp_pmp450i.py tests/test_inventory.py tests/test_register_device.py::test_register_device_routes_through_wrapper -v` → green; back-compat preserved.

**Work-unit boundary**: single commit `refactor(driver): route Inventory.get() through MutableInventory`. Justified as a single commit because the wrapper is the only invariant change — 8 read-sites stay textually identical.
**LoC estimate**: +5 production, +25 tests.
**Dependencies**: Task 2.

---

## 4. Implement `register_device` MCP tool + `DeviceRecord`

**Spec scenarios covered**: R-NEW-1-S2, R-NEW-2-S2, R-NEW-2-S3, Tool-S1, Tool-S2, Tool-S3, Tool-S4, Tool-S5, Tool-S6
**Design ref**: §4.3, §5

**Files touched**:
- new: `src/nora/drivers/snmp_pmp450i/register_device.py` (~90 lines)
- new: `tests/test_register_device.py` (~140 lines; 7 scenarios + Tool-S7 pinned here)

**Strict TDD sequence**:
1. Test: `test_register_device_success_with_validate` — fake `SnmpClient.get_oid("1.3.6.1.2.1.1.1.0")` returns `b"Cambium PMP 450i ..."`; tool returns typed `DeviceRecord`; `MutableInventory.get(device_id)` returns the device.
2. Test: `test_register_device_rejects_unreachable_host` — fake raises `OSError`; `DeviceUnreachable("192.0.2.10")` raised; `MutableInventory.device_ids` empty.
3. Test: `test_register_device_validate_false_inserts_without_wire` — `validate=False`; assert `client.get_oid` never called (`assert_not_called`); device inserted.
4. Test: `test_register_device_rejects_malformed_host` — `host="not-an-ip"`; `InvalidHostError` raised; no wire frame.
5. Test: `test_register_device_rejects_invalid_community` — fake raises `puresnmp.exc.SnmpError`; `InvalidCommunity("bogus-community")` raised.
6. Test: `test_register_device_two_calls_return_distinct_device_ids` — two consecutive `register_device(host="192.0.2.10", community="...", validate=False)`; both `device_id`s differ and both reachable via `MutableInventory.get(...)`.
7. Test: `test_register_device_payload_masks_credentials` — `DeviceRecord.model_dump(mode="json")` shows `"community": "**********"`; literal `"MEXI2-BB-RW"` absent.
8. Test: `test_register_device_free_text_error_message_is_sanitized` — `DeviceUnreachable` message carries `192.0.2.10`; tool serialisation replaces with synthetic alias; typed `device_id`/`host` byte-identical.
9. Implementation: `register_device.py` — `_register_device_impl(driver, host, community, validate, sanitizer)` + frozen Pydantic `DeviceRecord` (with `SecretStr` community). Body: `DeviceResolver.build(host, "v2c", SnmpCredentials(community))` → optional `sysDescr` GET → `driver.register(device)` → `model_dump(mode="json")`.
10. Verify: `uv run pytest tests/test_register_device.py -v` → 8 tests green.

**Work-unit boundary**: single commit `feat(mcp): register_device tool + DeviceRecord`. Production-only file in this step; wiring into `server.py` is Task 5.
**LoC estimate**: +90 production, +140 tests.
**Dependencies**: Tasks 1, 2, 3.

---

## 5. Wire `register_device` into `server.py` + rename `_eleven_tools` → `_twelve_tools`

**Spec scenarios covered**: R-NEW-1-S1, R-NEW-1-S2, R-NEW-2-S1
**Design ref**: §3 server.py, §4.3

**Files touched**:
- modified: `src/nora/server.py` (~15 lines: import `register_device`; `@mcp.tool register_device` decorator after line 469; add `"register_device"` to `__all__`; fix "five" → "twelve" docstring at line 4)
- modified: `tests/test_server.py` (~10 lines: rename `test_server_exposes_exactly_eleven_tools` → `_twelve_tools`; add `"register_device"` to expected set)

**Strict TDD sequence**:
1. Test: `tests/test_server.py::test_server_exposes_exactly_twelve_tools` — `nora.server.__all__` carries all 12 names including `register_device`; count assertion now 12.
2. Test: `tests/test_register_device.py::test_register_device_tools_list_schema` — boots `mcp.run()` over stdio; `tools/list` returns `register_device` with `inputSchema` declaring `host: str`, `community: str`, `validate: bool` (default `true`).
3. Implementation: in `server.py`, add `from nora.drivers.snmp_pmp450i.register_device import register_device` (or `register_device as _register_device_tool`); apply `@mcp.tool` (mirroring the 11 existing tools); append to `__all__`; fix module docstring count.
4. Verify: `uv run pytest tests/test_server.py::test_server_exposes_exactly_twelve_tools tests/test_register_device.py::test_register_device_tools_list_schema -v` → green.

**Work-unit boundary**: single commit `feat(server): register_device wired + twelve-tool surface`. Bundled with the rename because the test breaks on the FIRST run after the tool is registered — must land in the same commit, per explore §7.2.
**LoC estimate**: +15 production, +20 tests (rename + schema test).
**Dependencies**: Task 4.

---

## 6. Re-sign PMP 450i baselines (operator root + built-in root) + edit `TOOLS_V1`

**Spec scenarios covered**: R-NEW-6-S2, R-NEW-6-S3, R-NEW-6-S4
**Design ref**: §3 (catalog files), §8 (procedure)

**Files touched**:
- modified: `scripts/sign_catalog.py` (~3 lines: add `"register_device": ["sysDescr"]` to `TOOLS_V1` at line 80)
- modified: `data/oid-catalogs/sources/cambium/pmp450i/{15.2.1,15.3.0,25.1.0}.source.json` (3 files; +1 line each: `"sysDescr": "1.3.6.1.2.1.1.1.0"` to `oids`)
- generated (operator root): `data/oid-catalogs/cambium/pmp450i/{15.2.1,15.3.0,25.1.0}.json` (re-signed; HMAC + envelope change)
- generated (built-in root): `src/nora/data/oid-catalogs/pmp450i/{15.2.1,15.3.0,25.1.0}.json` (re-signed; **mandatory** — without this boot aborts on `CatalogVerificationError`, per design §8 step 4)
- modified: `tests/conftest.py` (~2 lines: add `"sysDescr"` to `_SAMPLE_CATALOG_PAYLOAD`)
- modified: `tests/test_oid_catalog_integration.py` (~5 lines: add `"register_device": ["sysDescr"]` to `_INTEGRATION_CATALOG_TOOLS`)

**Strict TDD sequence**:
1. Test: `tests/test_oid_catalog_integration.py::test_every_resigned_baseline_tools_envelope_includes_register_device` — parametrized across `("cambium","pmp450i","15.2.1")`, `("cambium","pmp450i","15.3.0")`, `("cambium","pmp450i","25.1.0")`; each catalog's `tools` envelope contains `"register_device": ["sysDescr"]`.
2. Test: `test_resigned_baseline_oids_includes_sysdescr` — parametrized; each catalog's `oids` map contains `"sysDescr": "1.3.6.1.2.1.1.1.0"`.
3. Test: `test_every_resigned_baseline_hmac_verifies` — parametrized; `OidCatalogRegistry.verify_all()` passes for all three baselines.
4. Test: `test_register_device_passes_registration_guard_via_catalog_envelope` — `cli.main()` runs the registration guard; `register_device` accepted without allow-list; `mcp.run(show_banner=False)` proceeds.
5. Implementation: edit `scripts/sign_catalog.py::TOOLS_V1`; edit 3 source.json files; run `NORA_OID_CATALOG_SIGNING_KEY="$BUILTIN_BASELINE_SIGNING_KEY" python scripts/sign_catalog.py --all --output-root src/nora/data/oid-catalogs`; then `NORA_OID_CATALOG_SIGNING_KEY="$OPERATOR_KEY" python scripts/sign_catalog.py --all` (operator root).
6. Verify: `uv run pytest tests/test_oid_catalog_integration.py -v` → parametrized tests green; HMAC verifies across all 3 baselines.

**Work-unit boundary**: single commit `feat(catalog): re-sign PMP 450i baselines + register_device envelope`. The two re-signs are bundled into the same commit because they MUST land together (design §8 step 4: built-in re-sign required or boot aborts). Re-signed JSON files are generated goldens — they appear in the diff but are excluded from the authored 400-line count per work-unit-commits skill.
**LoC estimate**: +8 production (script + 3 source.json), +12 tests; 12 generated golden files (excluded from authored count).
**Dependencies**: Task 5.

---

## 7. Retire `snmp_get_pmp450i_radio_metrics` from `_ALLOWED_UNCATALOGUED_TOOLS`

**Spec scenarios covered**: R-NEW-6-S5
**Design ref**: §3 (server.py:626), explore §2.3

**Files touched**:
- modified: `src/nora/server.py` (~3 lines: drop `"snmp_get_pmp450i_radio_metrics"` from the frozenset at line 626; verify `snmp_get_pmp450i_radio_metrics` is added to all three re-signed catalogs in Task 6 — bundled with the same re-sign)

**Strict TDD sequence**:
1. Test: `tests/test_server.py::test_snmp_get_pmp450i_radio_metrics_no_longer_in_allowlist` — inspect `_ALLOWED_UNCATALOGUED_TOOLS` at `src/nora/server.py:626`; assert `"snmp_get_pmp450i_radio_metrics"` NOT in set; the four remaining entries unchanged.
2. Implementation: edit `server.py:626` to drop the entry. (The catalog envelope addition for `snmp_get_pmp450i_radio_metrics` itself is part of Task 6's re-sign step — bundled here as a separate commit for review granularity since the rationale is tech-debt cleanup, not feature delivery.)
3. Verify: `uv run pytest tests/test_server.py::test_snmp_get_pmp450i_radio_metrics_no_longer_in_allowlist -v` → green.

**Work-unit boundary**: single commit `refactor(server): retire snmp_get_pmp450i_radio_metrics from uncatalogued allow-list`. Kept separate from Task 6 so the re-sign diff and the allow-list diff are independently auditable.
**LoC estimate**: +3 production, +15 tests.
**Dependencies**: Task 6 (catalog must carry `snmp_get_pmp450i_radio_metrics` before the allow-list entry can be safely removed).

---

## 8. Add `data/devices.yaml` to `.gitignore` + remove stale claim from `devices.example.yaml`

**Spec scenarios covered**: Gitignore-S1, Gitignore-S2, Gitignore-S3
**Design ref**: §3 (.gitignore + devices.example.yaml); explore §5.2

**Files touched**:
- modified: `.gitignore` (+4 lines: `# Operator-supplied device inventory (contains SNMP credentials).`, `data/devices.yaml`, `data/devices-*.yaml`, `!data/devices.example.yaml`)
- modified: `data/devices.example.yaml` (~-1 line: drop the false "gitignored" claim at line 5)
- new: `tests/test_devices_yaml_gitignore.py` (~30 lines, 3 scenarios)

**Strict TDD sequence**:
1. Test: `tests/test_devices_yaml_gitignore.py::test_data_devices_yaml_is_gitignored` — `git check-ignore -v data/devices.yaml` exits 0 and names a matching `.gitignore` line.
2. Test: `test_data_devices_example_yaml_remains_tracked` — `git check-ignore -v data/devices.example.yaml` exits 1 (NOT ignored).
3. Test: `test_stale_gitignored_claim_removed_from_devices_example_yaml` — line 5 of `data/devices.example.yaml` does NOT contain the substring `gitignored`.
4. Implementation: append the three `.gitignore` lines; delete the stale claim from `devices.example.yaml:5`.
5. Verify: `uv run pytest tests/test_devices_yaml_gitignore.py -v` → 3 tests green.

**Work-unit boundary**: single commit `chore: gitignore data/devices.yaml + fix stale devices.example.yaml`. Independent of all other tasks.
**LoC estimate**: +3 production (.gitignore) −1 production (devices.example.yaml), +30 tests.
**Dependencies**: none.

---

## 9. Update §4 Step 4 of `netops_orchestrator.md` with `register_device` fallback clause

**Spec scenarios covered**: Prompt-S1, Prompt-S2, Prompt-S3
**Design ref**: §3 (prompts/netops_orchestrator.md)

**Files touched**:
- modified: `src/nora/prompts/netops_orchestrator.md` (+~10 lines: new fallback clause inside §4 Step 4 after line 48 — when `device_id` looks like IPv4 AND `Inventory.get` raises `DeviceNotFoundError`, call `register_device(host, community)`; references `sysDescr` validation contract; explicitly does NOT instruct the orchestrator to echo `community`)
- new: `tests/test_prompts_register_device_clause.py` (~30 lines, 3 regex scenarios)

**Strict TDD sequence**:
1. Test: `test_prompts_register_device_clause.py::test_mentions_register_device_in_section_4` — regex scan between `## 4.` and `## 5.` headings in `src/nora/prompts/netops_orchestrator.md` finds at least one `register_device` literal.
2. Test: `test_references_sysdescr_validation_contract` — regex finds `sysDescr` OR `1.3.6.1.2.1.1.1.0` in §4.
3. Test: `test_does_not_instruct_orchestrator_to_echo_community` — regex for `echo` followed by `community` (case-insensitive) inside §4 returns zero matches.
4. Implementation: append the fallback clause to §4 Step 4.
5. Verify: `uv run pytest tests/test_prompts_register_device_clause.py -v` → 3 tests green.

**Work-unit boundary**: single commit `docs(prompt): §4 Step 4 register_device fallback clause`. Docs live with the feature they describe — per work-unit-commits skill.
**LoC estimate**: +10 production (markdown), +30 tests.
**Dependencies**: none.

---

## 10. Final integration test + full suite (`pytest` + `ruff` + `mypy --strict`)

**Spec scenarios covered**: R-NEW-6-S1 (boot guard rejects rogue tool); integration cross-check of all 11 prior tasks
**Design ref**: §9 rollout step 8

**Files touched**:
- modified: `tests/test_integration_boot.py` or `tests/test_integration.py` (~+20 lines: end-to-end boot via `cli.main()`; assert all 12 tools listed; assert `register_device` round-trip succeeds against snmpsim v2c agent for `192.0.2.10`)

**Strict TDD sequence**:
1. Test: `tests/test_integration.py::test_boot_with_register_device_round_trip` — `cli.main()` runs; boots `mcp`; `tools/list` returns 12; call `register_device(host="192.0.2.10", community="MEXI2-BB-RW", validate=True)` against snmpsim → device inserted; subsequent `snmp_get_ap_summary(device_id=<returned>)` returns typed payload.
2. Test: `tests/test_integration.py::test_rogue_tool_rejected_at_boot` — declare a rogue `@mcp.tool snmp_get_rogue_metric(...)` with no catalog entry; `cli.main()` raises `UncataloguedToolError(tool_name="snmp_get_rogue_metric", reason="no OID catalog entry")` BEFORE `mcp.run(...)`.
3. Implementation: write the two integration tests; no production code changes here.
4. Verify: `uv run python -m pytest --cov=src/nora --cov-report=term-missing` exits 0 with coverage ≥85%; `uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src/nora` exits 0.

**Work-unit boundary**: single commit `test(integration): register_device round-trip + boot guard E2E`. Aggregates evidence across all 9 prior tasks.
**LoC estimate**: +40 tests; +0 production.
**Dependencies**: Tasks 1–9 (all prior tasks must be green before this lands).

---

## 11. Manual sanity check against a real Cambium PMP 450i radio (operator-only, optional)

**Spec scenarios covered**: operational confidence (out-of-band)
**Design ref**: §9 rollout step (operator-side)

**Files touched**: none.

**Work-unit boundary**: documented in PR body, not a commit. Operator-side step.
**Dependencies**: Task 10 (PR merged to `main`).

**Procedure** (operator-only, post-merge):
1. Build wheels and install on the operator's host.
2. Boot against the real PMP 450i radio at the operator's tower.
3. Confirm orchestrator → `register_device("10.53.20.5", "MEXI2-BB-RW")` succeeds and the subsequent `snmp_get_ap_summary` returns firmware + carrier freq.
4. Confirm `data/devices.yaml` is NOT modified at runtime (YAML persistence out of scope per explore §6).
5. Confirm `git status` shows no `data/devices.yaml` tracked (gitignore fix from Task 8).

---

## Review Workload Forecast

**Estimated total LoC**: ~420 (280 production + 140 tests). Excludes 12 re-signed catalog JSON goldens per work-unit-commits skill.
**Tasks count**: 11 (10 work-unit commits + 1 manual operator step).
**Estimated changed files**: 6 new (`mutable_inventory.py`, `register_device.py`, `test_mutable_inventory.py`, `test_register_device.py`, `test_devices_yaml_gitignore.py`, `test_prompts_register_device_clause.py`) + 12 modified entries (per design §3 table).
**Chained PRs recommended**: No (per orchestrator preflight `single-pr` strategy).
**400-line budget risk**: Medium (~420 is just over 400 — accepted per orchestrator preflight `review_budget_lines=800`).
**800-line budget risk**: Low (420 well under 800).
**Decision needed before apply**: No (per orchestrator preflight `single-pr` — size:exception NOT required because 420 < 800).
**Justification**: All work fits inside the 800-line review budget the operator preflighted. The five production + six test files each carry their own RED → GREEN sequence, so the diff tells a coherent feature story (foundation → driver migration → tool → wiring → catalog → cleanup → docs → integration). The catalog re-sign is bundled into one commit because the built-in root re-sign is mandatory in the same PR (without it, boot aborts per design §8 step 4). No coupling forces a chained-PR split.

---

## Risks

| # | Risk | Source | Surface |
|---|------|---------|---------|
| R1 | Catalog re-sign key drift — operator key mismatch invalidates all 3 baselines and breaks boot. | design §10 risk #2 | Tasks 6 + 11. Mitigation: `OidCatalogRegistry.verify_all()` runs as a parametrized test (Cat-S1) on every CI run. |
| R2 | MutableInventory wrapper drift — a missed `Inventory.get()` bypass site breaks overlay silently. | design §10 risk #1 | Task 3. Mitigation: `test_register_device_routes_through_wrapper` (Tool-S7) pins the path; full `test_driver_snmp_pmp450i.py` back-compat suite verifies zero drift. |
| R3 | `_ALLOWED_UNCATALOGUED_TOOLS` removal lands before catalog re-sign — boot aborts. | design §3 | Tasks 6 → 7 ordering enforced by task dependency graph. Mitigation: Task 7 explicitly depends on Task 6. |
| R4 | HMAC key leakage in PR body / CI logs. | operational | Task 6. Mitigation: keys read from env (`NORA_OID_CATALOG_SIGNING_KEY`), never committed; CI uses secret manager. |
| R5 | Prompt clause heuristic bypass under edge cases — orchestrator falls back to asking for `device_id`. | design §10 risk #4 | Task 9. Mitigation: failure mode is identical to today's (`DeviceNotFoundError` returned); no regression. |
| R6 | Thread-safety of `MutableInventory._overlay` under concurrent `register_device` calls. | design §10 risk #3 | Task 2. Mitigation: `threading.Lock` serializes mutators; reads lock-free. `Device` itself stays Pydantic-frozen. |

---

## Out-of-scope tasks

Items deferred to follow-up changes (per design §11 "Open questions" + explore §6):

- **YAML persistence** of runtime-registered devices back to `data/devices.yaml` (atomic write + reload semantics).
- **Hot-reload** of `data/devices.yaml` after manual operator edit (spec currently forbids one-shot boot scan).
- **Firmware auto-pin** at registration time — `DeviceResolver.build` leaves `firmware="(adhoc)"`; next telemetry call overwrites.
- **Batch import** — one device per `register_device` call.
- **Discovery protocols** — CDP/LLDP/DHCP snooping.
- **GUI inventory editor** — operators edit `data/devices.yaml` directly.
- **`NORA_DEFAULT_SNMP_COMMUNITY`** env var — explicitly rejected (community must flow operator → chat → tool).
- **Optional `community` parameter on existing diagnostic tools** — explicitly rejected (leaks credentials into model context).
- **Resyncing the 6 other tools (ap_summary, frame_utilization, sm_table, sm_detailed_diagnostics, spectrum, migrate) into §4 of `netops_orchestrator.md`** — pre-existing drift, separate change.
- **Behavioral prompt test in `tests/test_prompts.py`** — out of scope (per design §3 note: prompt tests are content-based regex, not behavioural).
- **v3 (USM) credential support** in `register_device` — only `v2c` covered per spec (design §1 non-goals).
- **Bringing §4 prompt fully in sync with all 12 tools** — only Step 4 fallback added.