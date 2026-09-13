# Tasks: PMP 450i Production Surface (Cluster #14 + #15 + #24)

> **Branch chain (stacked-to-main, auto-chain)**: PR 1 → main → PR 2 → main → PR 3 → main → PR 4 → main → PR 5 → main. PR 5 carries `Closes #14`, `Closes #15`, `Closes #24`. Test runner: `uv run python -m pytest --cov=src/nora --cov-report=term-missing`. Coverage threshold: 85%. Strict TDD (RED → GREEN → REFACTOR) inside every slice.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | ~2,200 across 5 PRs |
| 400-line budget risk | Low (per-PR ≤800 LOC preflight) |
| Chained PRs recommended | Yes (auto-chain per orchestrator preflight) |
| Delivery strategy | auto-chain |
| Chain strategy | stacked-to-main |
| Decision needed before apply | No (auto-chain + stacked-to-main preflight cached) |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: Low

### Per-PR Budget Claims

| PR | Slice | Estimated LOC (impl + tests) | ≤800 LOC? |
|----|-------|------------------------------|-----------|
| 1 | DeviceDriverInterface seam + IP-direct resolver + report_firmware | ~560 (220 impl + 340 tests) | yes |
| 2 | ap_summary + frame_utilization | ~360 (140 impl + 220 tests) | yes |
| 3 | sm_table + sm_detailed_diagnostics + categorize_subscribers | ~470 (200 impl + 270 tests) | yes |
| 4 | spectrum + HITL-gated migration + watchdog | ~790 (350 impl + 440 tests) | yes (tight) |
| 5 | tool-registration guard + E2E integration test | ~370 (100 impl + 270 tests) | yes |

PR 4 is the tightest slice; if its diff exceeds 800 LOC the orchestrator halts the chain and requests `size:exception` per the 800-line preflight guard.

---

## PR 1 — DeviceDriverInterface Seam + IP-Direct Resolver

Files (NEW in caps): `src/nora/drivers/INTERFACE.py`, `src/nora/drivers/RESOLVER.py`; MOD `src/nora/drivers/snmp_pmp450i/driver.py` (add `report_firmware`, expose `Pmp450iSnmpDriver`), `src/nora/drivers/REGISTRY.py` (accept new type), `src/nora/drivers/EXCEPTIONS.py` (add `AutonomousMutationRejected`, `MaintenanceWindowViolation`, `UncataloguedToolError`); NEW tests `tests/TEST_DRIVER_INTERFACE.py`, `tests/TEST_RESOLVER.py`; MOD `tests/test_driver_airgap.py` (cover `drivers/resolver.py`). Branch: `pr1/driver-interface-seam`. Commit prefix: `feat(driver-interface):`. Satisfies named tests: `ip_direct_resolution_builds_ephemeral_device`, `secret_str_safety_no_plaintext_in_repr`, `report_firmware_returns_typed_version`, `inventory_path_still_works_no_regression`.

### Phase 1.1 — Manifest + Branch

- [ ] 1.1 Create branch `pr1/driver-interface-seam` from `main`; verify `pyproject.toml::[tool.hatch.build.targets.wheel].packages = ["src/nora"]` already covers `src/nora/drivers/resolver.py` (wildcard — no edit).
- [ ] 1.2 Extend `tests/test_driver_airgap.py::_iter_python_files` to confirm `drivers/resolver.py` is in the AST-walked set.

### Phase 1.2 — RED (Protocol + Resolver + Firmware)

- [ ] 1.3 RED `tests/test_driver_interface.py::ip_direct_resolution_builds_ephemeral_device` — `DeviceResolver.build("192.0.2.10","v3", creds)` returns frozen `Device` with `device_id == "adhoc-192.0.2.10-<6hex>"`.
- [ ] 1.4 RED `tests/test_driver_interface.py::secret_str_safety_no_plaintext_in_repr` — `repr(device)` and `device.model_dump()` contain `**********` and never `community.get_secret_value()`.
- [ ] 1.5 RED `tests/test_driver_interface.py::report_firmware_returns_typed_version` — `Pmp450iSnmpDriver.report_firmware("ap-7400-01")` returns `packaging.version.Version("15.3.0")` for snmpsim agent.
- [ ] 1.6 RED `tests/test_resolver.py::inventory_path_still_works_no_regression` — existing `tests/test_driver_snmp_pmp450i.py`, `tests/test_driver_snmpsim_{v2c,v3}.py`, `tests/test_driver_snmp450i_readonly.py`, `tests/test_driver_airgap.py` all pass with the seam in place.

### Phase 1.3 — GREEN

- [ ] 1.7 GREEN `src/nora/drivers/interface.py` — `DeviceDriverInterface(Protocol, runtime_checkable)` with six methods (`fetch_radio_metrics`, `fetch_ap_summary`, `fetch_frame_utilization`, `fetch_sm_table`, `fetch_sm_detailed_diagnostics`, `report_firmware`).
- [ ] 1.8 GREEN `src/nora/drivers/resolver.py` — `DeviceResolver.build(host, snmp_version, creds) -> Device`; stem `f"adhoc-{host}-{secrets.token_hex(3)}"`; never mutates `Settings.nora_devices_inventory_path`.
- [ ] 1.9 GREEN `src/nora/drivers/snmp_pmp450i/driver.py` — add `Pmp450iSnmpDriver(Pmp450iDriver)` with `report_firmware(device_id) -> Version` (parses `sysDescr`); existing `fetch_radio_metrics` preserved byte-identical.
- [ ] 1.10 GREEN `src/nora/drivers/exceptions.py` — add `AutonomousMutationRejected`, `MaintenanceWindowViolation`, `UncataloguedToolError` (typed, RFC 2119 messages).
- [ ] 1.11 GREEN `src/nora/drivers/registry.py` — broaden `_driver` type to `Pmp450iSnmpDriver`; preserve `set_driver/get_driver` API.

### Phase 1.4 — REFACTOR + Verify + Merge

- [ ] 1.12 REFACTOR type hints, hoisted `_COLLISION_FREE_HOST_RE`, frozen `Device` re-export in `nora.drivers.__init__`.
- [ ] 1.13 Verify: `uv run python -m pytest --cov=src/nora --cov-report=term-missing` exits 0 (coverage ≥85%); `uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src/nora` exits 0. Rollback: revert merge — `DeviceResolver.build` paths unused, registry keeps `Pmp450iDriver`. Merge PR → main.

---

## PR 2 — Read Summaries (`ap_summary` + `frame_utilization`)

Files (NEW in caps): `src/nora/drivers/snmp_pmp450i/SUMMARIES.py`; MOD `src/nora/server.py` (+2 `@mcp.tool` wrappers), `src/nora/drivers/oid_catalog.py` (extend envelope `"tools"` map); NEW test `tests/TEST_SNMP_SUMMARIES.py`; catalog re-sign. Branch: `pr2/read-summaries`. Commit prefix: `feat(radio-tools):`. Satisfies named tests: `ap_summary_returns_typed_model`, `frame_utilization_returns_typed_model`, `unknown_oid_warn_and_value`, `catalog_minor_mismatch_warning_via_summary_call`.

### Phase 2.1 — Branch + Catalog

- [ ] 2.1 Create branch `pr2/read-summaries` from `main`. Re-run `scripts/sign_catalog.py --vendor cambium --model pmp450i --firmware 15.2.1` after extending `OID_CATALOG_V1` with summary OIDs (`apFirmwareVersion`, `frameUtilizationDlPct`, `frameUtilizationUlPct`) and the `"tools": {"snmp_get_ap_summary": [...], "snmp_get_frame_utilization": [...]}` envelope.

### Phase 2.2 — RED

- [ ] 2.2 RED `tests/test_snmp_summaries.py::ap_summary_returns_typed_model` — `snmp_get_ap_summary("ap-7400-01")` returns `ApSummary.model_dump()` against snmpsim agent with `firmware==15.2.1`.
- [ ] 2.3 RED `frame_utilization_returns_typed_model` — `snmp_get_frame_utilization` returns typed floats for downlink/uplink percentages.
- [ ] 2.4 RED `unknown_oid_warn_and_value` — catalog missing one AP-summary OID → literal `OID catalog fallback` warning on stderr AND the missing field is `None`.
- [ ] 2.5 RED `catalog_minor_mismatch_warning_via_summary_call` — registry holds `15.2.1`+`15.3.0`, agent reports `15.3.1` → uses `15.3.0` AND stderr carries literal `"OID catalog fallback: requested 15.3.1, using 15.3.0 (minor mismatch)"`.

### Phase 2.3 — GREEN

- [ ] 2.6 GREEN `src/nora/drivers/snmp_pmp450i/summaries.py` — `ApSummary`, `FrameUtilization` Pydantic models; `fetch_ap_summary(device_id)`, `fetch_frame_utilization(device_id)` call `OidCatalogRegistry.resolve(...)` before wire frame.
- [ ] 2.7 GREEN `src/nora/server.py` — add `@mcp.tool snmp_get_ap_summary(device_id: str) -> dict` and `@mcp.tool snmp_get_frame_utilization(device_id: str) -> dict`; free-text fields pass `_sanitizer.sanitize(...)`; `__all__` extended.
- [ ] 2.8 GREEN `src/nora/drivers/oid_catalog.py` — extend catalog JSON envelope with `"tools"` map (two entries); `_REQUIRED_OIDS_BY_VENDOR_MODEL["cambium","pmp450i"]` augmented with the summary OID names.

### Phase 2.4 — REFACTOR + Verify + Merge

- [ ] 2.9 REFACTOR extract `_resolve_and_fetch(device_id, fetcher)` helper inside `summaries.py`; no duplication between the two tools.
- [ ] 2.10 Verify: full pytest exits 0 with coverage ≥85%; ruff + format + mypy `--strict` exit 0. Rollback: revert merge — two `@mcp.tool`s + `summaries.py` removed; catalog re-signed to original 15-OID seed. Merge PR → main.

---

## PR 3 — Subscriber Baseline + Per-LUID Diagnostics

Files (NEW in caps): `src/nora/drivers/snmp_pmp450i/SUBSCRIBERS.py`; MOD `src/nora/server.py` (+2 `@mcp.tool` wrappers); NEW test `tests/TEST_SNMP_SUBSCRIBERS.py`; catalog re-sign. Branch: `pr3/subscribers-and-categorize`. Commit prefix: `feat(radio-tools):`. Satisfies named tests: `sm_table_categorizes_online_active`, `sm_table_categorizes_active_degraded_low_cinr`, `sm_table_categorizes_pre_existing_offline`, `sm_table_unbiased_baseline_excludes_pre_existing`, `sm_detailed_diagnostics_typed_for_luid`, `get_intervention_history_called_before_categorize`.

### Phase 3.1 — Branch + Catalog

- [ ] 3.1 Create branch `pr3/subscribers-and-categorize` from `main`. Re-sign `scripts/sign_catalog.py --vendor cambium --model pmp450i --firmware 15.2.1` adding SM table OIDs (`smSessionUptime`, `smCinr`, `smLinkStatus`, `smLuid`) and diagnostics OIDs (`smJitter`, `smRetransmits`, `smRxLevel`, `smTxLevel`); add `"tools": {"snmp_get_sm_table": [...], "snmp_get_sm_detailed_diagnostics": [...]}` to the envelope.

### Phase 3.2 — RED

- [ ] 3.2 RED `tests/test_snmp_subscribers.py::sm_table_categorizes_online_active` — fixture with three SM rows; row with `session_uptime>0` AND linked modulation categorizes `ONLINE_ACTIVE`.
- [ ] 3.3 RED `sm_table_categorizes_active_degraded_low_cinr` — row with `cinr<18` categorizes `ACTIVE_DEGRADED`.
- [ ] 3.4 RED `sm_table_categorizes_pre_existing_offline` — row whose `luid` is in `known_pre_existing_offline_subscribers` categorizes `PRE_EXISTING_OFFLINE`.
- [ ] 3.5 RED `sm_table_unbiased_baseline_excludes_pre_existing` — 100 SMs (60 ONLINE_ACTIVE, 30 ACTIVE_DEGRADED, 10 PRE_EXISTING_OFFLINE) → tool returns 90 candidate rows AND `pre_existing_offline_count == 10`.
- [ ] 3.6 RED `sm_detailed_diagnostics_typed_for_luid` — `snmp_get_sm_detailed_diagnostics(luid="...sm-7400-02")` returns typed `SmDetailedDiagnostics` with jitter, CINR, Rx/Tx levels, retransmits, interface error counters.
- [ ] 3.7 RED `get_intervention_history_called_before_categorize` — monkeypatch `search_intervention_history` to raise if called AFTER `categorize_subscribers`; tool body reorders → typed error names the missing prerequisite call.

### Phase 3.3 — GREEN

- [ ] 3.8 GREEN `src/nora/drivers/snmp_pmp450i/subscribers.py` — `categorize_subscribers(sm_rows, intervention_history) -> Literal["ONLINE_ACTIVE","ACTIVE_DEGRADED","PRE_EXISTING_OFFLINE"]`; exclusion FIRST via `known_pre_existing_offline_subscribers`; cross-check guard via `OrderedDict` insertion sequence.
- [ ] 3.9 GREEN `src/nora/drivers/snmp_pmp450i/subscribers.py` — `SubscriberSummary`, `SmDetailedDiagnostics` Pydantic models; `fetch_sm_table(target_ip, settings)` and `fetch_sm_detailed_diagnostics(luid)` call `search_intervention_history(stage="PRE_DIAGNOSTIC")` BEFORE `categorize_subscribers(...)`.
- [ ] 3.10 GREEN `src/nora/server.py` — add `@mcp.tool snmp_get_sm_table(target_ip: str)` and `@mcp.tool snmp_get_sm_detailed_diagnostics(luid: str)`; free-text fields sanitized; `__all__` extended.

### Phase 3.4 — REFACTOR + Verify + Merge

- [ ] 3.11 REFACTOR centralise `InterventionMemoryRecord` access via `nora.intervention_memory.tools.get_device_lifecycle_summary`; reject direct disk reads in this module (AST guard check).
- [ ] 3.12 Verify: full pytest exits 0 with coverage ≥85%; ruff + format + mypy `--strict` exit 0. Rollback: revert merge — `subscribers.py` + 2 tools removed; catalog re-signed without SM OIDs. Merge PR → main.

---

## PR 4 — Spectrum + HITL-Gated Migration (8 named tests, meatiest slice)

Files (NEW in caps): `src/nora/hitl/TOKENS.py`, `src/nora/drivers/snmp_pmp450i/SPECTRUM.py`, `src/nora/drivers/snmp_pmp450i/MIGRATE.py`; MOD `src/nora/server.py` (+2 `@mcp.tool` wrappers + HITL text in `_SERVER_INSTRUCTIONS`), `src/nora/config.py` (+ `nora_hitl_rollback_timeout_seconds=300`, `nora_hitl_token_ttl_seconds=900`); NEW tests `tests/TEST_HITL_TOKENS.py`, `tests/TEST_SNMP_SPECTRUM.py`, `tests/TEST_SNMP_MIGRATE.py`; catalog re-sign. Branch: `pr4/spectrum-and-hitl-migrate`. Commit prefix: `feat(radio-tools):` (HITL commits use `feat(hitl):` and `feat(migrate):` for review granularity). Satisfies named tests: `spectrum_returns_ranked_clean_frequencies`, `spectrum_respects_maintenance_window`, `migrate_requires_hitl_approval_token`, `migrate_make_before_break_migrates_online_active_first`, `migrate_excludes_pre_existing_offline_subscribers`, `migrate_rolls_back_within_timeout_on_loss_of_management`, `migrate_autonomous_call_raises_autonomous_mutation_rejected`, `migrate_emits_intervention_record_on_completion`. **Backout escape hatch**: `NORA_HITL_TOKEN_TTL_SECONDS=0` makes the stub reject every token.

### Phase 4.1 — Branch + HITL Contract (commit 1 of 3 for slice 4)

- [ ] 4.1 Create branch `pr4/spectrum-and-hitl-migrate` from `main`.
- [ ] 4.2 RED `tests/test_hitl_tokens.py::migrate_autonomous_call_raises_autonomous_mutation_rejected` — `verify_approval_token(None)` raises `AutonomousMutationRejected("autonomous device mutation rejected: HITL approval token required")`.
- [ ] 4.3 RED `tests/test_hitl_tokens.py::migrate_requires_hitl_approval_token` — `verify_approval_token("expired-or-bogus")` raises the same typed exception; no `Settings` side-effects.
- [ ] 4.4 GREEN `src/nora/hitl/tokens.py` — `verify_approval_token(token: str | None, *, settings: Settings) -> None`; on `None`/empty/invalid → raises `AutonomousMutationRejected(...)` (stub: full state machine lands in next Phase-3 cluster).
- [ ] 4.5 GREEN `src/nora/config.py` — add `nora_hitl_rollback_timeout_seconds: int = 300` and `nora_hitl_token_ttl_seconds: int = 900` (operational backout: setting TTL=0 makes the stub reject every token).
- [ ] 4.6 REFACTOR extract `_is_valid_token_format(token)` so the stub is one line; commit as `feat(hitl): stub verifier + Settings.nora_hitl_*`.

### Phase 4.2 — Spectrum Tool (commit 2 of 3 for slice 4)

- [ ] 4.7 RED `tests/test_snmp_spectrum.py::spectrum_returns_ranked_clean_frequencies` — snmpsim agent with three noise floors; tool returns ranked list ordered by lowest noise floor first.
- [ ] 4.8 RED `tests/test_snmp_spectrum.py::spectrum_respects_maintenance_window` — `now` outside `Settings.nora_maintenance_window_*` → `MaintenanceWindowViolation` raised; zero SNMP frames sent (mock assert `assert_not_called`).
- [ ] 4.9 GREEN `src/nora/drivers/snmp_pmp450i/spectrum.py` — `SpectrumCandidate` Pydantic model; `fetch_spectrum(device_id, *, settings)` enforces maintenance window then triggers spectrum sweep.
- [ ] 4.10 GREEN `src/nora/server.py` — add `@mcp.tool snmp_run_spectrum_analysis(device_id: str)`; sanitise free-text; `__all__` extended.
- [ ] 4.11 GREEN catalog re-sign adding `spectrumNoiseFloor*` OIDs and `"tools": {"snmp_run_spectrum_analysis": [...]}`.
- [ ] 4.12 REFACTOR + commit as `feat(spectrum): snmp_run_spectrum_analysis + maintenance window guard`.

### Phase 4.3 — Migration Tool (commit 3 of 3 for slice 4)

- [ ] 4.13 RED `tests/test_snmp_migrate.py::migrate_make_before_break_migrates_online_active_first` — 10 ONLINE_ACTIVE, 5 ACTIVE_DEGRADED, 3 PRE_EXISTING_OFFLINE → order is `[online_active_sm_1..N, active_degraded_sm_1..M, ap_set]`.
- [ ] 4.14 RED `migrate_excludes_pre_existing_offline_subscribers` — `result["pre_existing_offline_excluded"] == 3`.
- [ ] 4.15 RED `migrate_rolls_back_within_timeout_on_loss_of_management` — watchdog times out after 300s (mocked `Timer`); prior carrier restored; `result == {"rolled_back": True, "reason": "loss_of_management"}`.
- [ ] 4.16 RED `migrate_emits_intervention_record_on_completion` — `save_intervention_record` called exactly once with `stage="POST_MIGRATION"`; on-disk file matches `INT-<ticket>-<ip>-<unix>-<6hex>.json`.
- [ ] 4.17 GREEN `src/nora/drivers/snmp_pmp450i/migrate.py` — `migrate_radio_frequency(device_id, *, approval_token, target_frequency_mhz, settings) -> dict`; order: ONLINE_ACTIVE → ACTIVE_DEGRADED → AP channel change LAST; `threading.Timer(settings.nora_hitl_rollback_timeout_seconds, _on_timeout)` armed AFTER AP SET; loss-of-mgmt → revert SET + emit `POST_MIGRATION` record with `rolled_back=True`.
- [ ] 4.18 GREEN `src/nora/drivers/oid_catalog.py` — add `resolve_migration_refs(current_ref, candidate_ref) -> tuple[OidCatalog, OidCatalog]` (raises `CatalogNotFoundError` if either fails; no SET frame sent by this method).
- [ ] 4.19 GREEN `src/nora/server.py` — add `@mcp.tool snmp_migrate_radio_frequency(device_id, approval_token, target_frequency_mhz)`; extend `_SERVER_INSTRUCTIONS` with HITL contract text on the migration tool; `__all__` extended.
- [ ] 4.20 REFACTOR + commit as `feat(migrate): snmp_migrate_radio_frequency + watchdog + intervention record emission`.

### Phase 4.4 — Verify + Merge

- [ ] 4.21 Verify: full pytest exits 0 with coverage ≥85%; ruff + format + mypy `--strict` exit 0. If diff >800 LOC → orchestrator halts chain and requests `size:exception`.
- [ ] 4.22 Rollback: revert merge — `hitl/tokens.py` + `nora_hitl_*` Settings removed; 2 `@mcp.tool`s deleted; catalog re-signed without spectrum/migration OIDs. Merge PR → main.

---

## PR 5 — Tool-Registration Guard + E2E Catalog Integration (Closes #14, #15, #24)

Files (NEW in caps): `tests/TEST_OID_CATALOG_INTEGRATION.py`; MOD `src/nora/CLI.py` (registration guard), `src/nora/drivers/oid_catalog.py` (per-tool index `REQUIRED_OIDS_BY_TOOL`), `src/nora/server.py` (verify 11 tools in `__all__`). Branch: `pr5/oid-catalog-integration`. Commit prefix: `feat(oid-catalog):` and `feat(cli):`. PR title: `feat(oid-catalog): registration guard + E2E integration test (closes #14, #15, #24)`. PR body: `Closes #14`, `Closes #15`, `Closes #24`. Satisfies named tests: `dynamic_resolution_applies_catalog_versioning_before_query`, `minor_mismatch_warning_during_read_path_e2e`, `major_mismatch_blocks_driver_query_typed`, `unified_tool_catalog_references_required_oids_per_tool`, `new_tool_without_oid_registration_rejected_at_registration_time`.

### Phase 5.1 — Branch + RED

- [ ] 5.1 Create branch `pr5/oid-catalog-integration` from `main`.
- [ ] 5.2 RED `tests/test_oid_catalog_integration.py::dynamic_resolution_applies_catalog_versioning_before_query` — `report_firmware()` returns `Version("15.3.0")`; resolver picks `15.2.1` (closest lower minor); wire OIDs equal `15.2.1`'s catalog entries.
- [ ] 5.3 RED `minor_mismatch_warning_during_read_path_e2e` — full E2E: `@mcp.tool` call → catalog resolver → driver wire; stderr carries literal `"OID catalog fallback: requested 15.3.0, using 15.2.1 (minor mismatch)"`.
- [ ] 5.4 RED `major_mismatch_blocks_driver_query_typed` — `report_firmware()` returns `Version("16.0.0")`; any read tool raises `CatalogNotFoundError` naming both majors.
- [ ] 5.5 RED `unified_tool_catalog_references_required_oids_per_tool` — boot validates per-tool index: every catalogued tool name resolves to a non-empty OID-name set; zero missing.
- [ ] 5.6 RED `new_tool_without_oid_registration_rejected_at_registration_time` — declare a rogue `@mcp.tool snmp_get_rogue_metric(...)` with NO catalog entry; `cli.main()` raises `UncataloguedToolError(tool_name="snmp_get_rogue_metric", reason="no OID catalog entry")` BEFORE `mcp.run(...)`.

### Phase 5.2 — GREEN

- [ ] 5.7 GREEN `src/nora/drivers/oid_catalog.py` — add `REQUIRED_OIDS_BY_TOOL: dict[tuple[str,str], dict[str, frozenset[str]]]` indexed at boot from the envelope `"tools"` map; exposed via `OidCatalogRegistry.required_oids_by_tool((vendor, model))`.
- [ ] 5.8 GREEN `src/nora/cli.py` — after `set_driver(...)`, walk the registered tools on `mcp` instance via `inspect` and raise `UncataloguedToolError` for any name absent from `REQUIRED_OIDS_BY_TOOL[(cambium, pmp450i)]`; abort before `mcp.run(...)`.
- [ ] 5.9 GREEN `src/nora/server.py` — confirm `__all__` enumerates all 11 tool names; update `tests/test_server.py::test_server_exposes_eleven_tools` (rename from `_four_tools` / `_five_tools`).
- [ ] 5.10 GREEN `tests/test_integration_boot.py`, `tests/test_integration.py`, `tests/test_main_alias.py`, `tests/test_prompts.py` — bump tool count assertion from 5 to 11.

### Phase 5.3 — REFACTOR + Verify + Merge + Close

- [ ] 5.11 REFACTOR per-tool index built once via `sorted(envelope["tools"].items())`; cache on `OidCatalogRegistry` instance; no disk walk on hot path.
- [ ] 5.12 Verify: full pytest exits 0 with coverage ≥85%; ruff + format + mypy `--strict` exit 0. CI green. Rollback: revert merge — guard + `REQUIRED_OIDS_BY_TOOL` removed; #24 stays open. Merge PR → main with `Closes #14, Closes #15, Closes #24`.

---

## Dependency Graph

```
PR 1: 1.1 → 1.3..1.6 (RED) → 1.7..1.11 (GREEN) → 1.12 → 1.13 → merge to main
PR 2: 2.1 → 2.2..2.5 (RED) → 2.6..2.8 (GREEN) → 2.9 → 2.10 → merge to main
PR 3: 3.1 → 3.2..3.7 (RED) → 3.8..3.10 (GREEN) → 3.11 → 3.12 → merge to main
PR 4: 4.1 → 4.2..4.5 (RED+GREEN: HITL commit)  ─┐
       4.6                                      │
       4.7..4.8 (RED) → 4.9..4.11 (GREEN) → 4.12 (spectrum commit)  ─┤
       4.13..4.16 (RED) → 4.17..4.19 (GREEN) → 4.20 (migrate commit) ┘
       4.21 → 4.22 → merge to main
PR 5: 5.1 → 5.2..5.6 (RED) → 5.7..5.10 (GREEN) → 5.11 → 5.12 → merge + Closes #14 #15 #24
```

Zero tasks run parallel across PRs. Each PR starts only after the previous merge is green on `main`. Within PR 4 the three commits land in order (HITL → spectrum → migrate) so reviewers can audit each piece independently.
