# Proposal: PMP 450i Production Surface (Cluster #14 + #15 + #24)

> **SCOPE.md phase**: Fase 2 (Driver Layer — `DeviceDriverInterface` seam); Fase 4 (FastMCP — 6 new `@mcp.tool`s). `openspec/config.yaml` HITL + Zero-Leakage govern slice 4. **PR 5 Closes #14, #15, #24.**

## Intent

Two walls: (1) ad-hoc equipment not in `devices.yaml` → `DeviceNotFoundError` (#14); (2) read-summary / subscriber / spectrum / migration flows only exist in OpenChat because the 6 radio-link tools are absent from NORA MCP (#15). ADR #17 Tests 9+10 require the driver to call `report_firmware()` and every tool to be OID-catalogued (#24). **1 cluster, 5 chained PRs (auto-chain).**

## Scope

**In**: `DeviceDriverInterface` Protocol; IP-direct `DeviceResolver`; 6 new `@mcp.tool`s; `tests/test_oid_catalog_integration.py` (ADR #17 Tests 9+10); HITL gate for `snmp_migrate_radio_frequency`. **Out**: real SSH/REST drivers (Protocol admits them, only SNMP ships); new vendors (Cambium PMP 450i only — second vendor waits per ADR #17 P3); key rotation, webui.db shim.

## Capabilities

### New
- **`driver-interface`** — Protocol + `DeviceResolver` + IP-direct path. → `openspec/specs/driver-interface/spec.md`.
- **`pmp450i-radio-tools`** — 6 typed `@mcp.tool`s (migration embeds HITL).
- **`oid-catalog-integration`** — ADR #17 Tests 9+10 + tool-registration guard rejecting uncatalogued tools at `mcp.tool()` time.

### Modified
- `driver-snmp-pmp450i` — backed by `DeviceDriverInterface`; adds `report_firmware()`; resolves catalog before every wire call.
- `oid-catalog` — `REQUIRED_OIDS` per-`(vendor, model)` plus per-tool; migration tool resolves TWO triples (current + candidate).
- `nora-mcp-server` — 5 → 11 tools; `instructions` advertises HITL on the migration tool.

## Slice Plan (auto-chain, 5 PRs, each ≤800 LOC)

| # | Slice — Issue mapping | Files (NEW in caps) | Closes |
|---|----------------------|---------------------|--------|
| 1 | **#14 s1**: `DeviceDriverInterface` + IP-direct SNMP + `report_firmware()` | `drivers/{INTERFACE,RESOLVER}.py`, `snmp_pmp450i/driver.py`, `registry.py`, `TEST_DRIVER_INTERFACE`, `TEST_RESOLVER` | — |
| 2 | **#15 c1**: `snmp_get_ap_summary` + `snmp_get_frame_utilization` (read summaries) | `snmp_pmp450i/SUMMARIES.py`, `server.py`, `TEST_SNMP_SUMMARIES`, catalog re-sign | — |
| 3 | **#15 c2**: `snmp_get_sm_table` + `snmp_get_sm_detailed_diagnostics` (subscribers; unbiased baseline) | `snmp_pmp450i/SUBSCRIBERS.py`, `server.py`, `TEST_SNMP_SUBSCRIBERS`, catalog re-sign | — |
| 4 | **#15 c3**: `snmp_run_spectrum_analysis` + `snmp_migrate_radio_frequency` (HITL) | `snmp_pmp450i/{SPECTRUM,MIGRATE}.py`, `hitl/TOKENS.py`, `server.py`, additive `Settings.nora_hitl_*`, `TEST_SNMP_SPECTRUM`, `TEST_SNMP_MIGRATE`, `TEST_HITL_TOKENS`, catalog re-sign | — |
| 5 | **#24**: `test_oid_catalog_integration.py` + tool-registration guard | `TEST_OID_CATALOG_INTEGRATION`, `server.py` guard | **Closes #14, #15, #24** |

**26 named tests (4+4+6+8+4)**: S1: `ip_direct_resolution_builds_ephemeral_device`, `secret_str_safety_no_plaintext_in_repr`, `report_firmware_returns_typed_version`, `inventory_path_still_works_no_regression`. S2: `ap_summary_returns_typed_model`, `frame_utilization_returns_typed_model`, `unknown_oid_warn_and_value`, `catalog_minor_mismatch_warning_via_summary_call`. S3: `sm_table_categorizes_{online_active,active_degraded_low_cinr,pre_existing_offline}`, `sm_table_unbiased_baseline_excludes_pre_existing`, `sm_detailed_diagnostics_typed_for_luid`, `get_intervention_history_called_before_categorize`. S4: `spectrum_returns_ranked_clean_frequencies`, `spectrum_respects_maintenance_window`, `migrate_requires_hitl_approval_token`, `migrate_make_before_break_migrates_online_active_first`, `migrate_excludes_pre_existing_offline_subscribers`, `migrate_rolls_back_within_timeout_on_loss_of_management`, `migrate_autonomous_call_raises_autonomous_mutation_rejected`, `migrate_emits_intervention_record_on_completion`. S5: `dynamic_resolution_applies_catalog_versioning_before_query`, `minor_mismatch_warning_during_read_path_e2e`, `major_mismatch_blocks_driver_query_typed`, `unified_tool_catalog_references_required_oids_per_tool`, `new_tool_without_oid_registration_rejected_at_registration_time`.

## HITL Plan — Slice 4 (`snmp_migrate_radio_frequency`)

- **Approval token**: every call MUST carry `approval_token: str` (full `ChangeRequest` state machine lands in next Phase-3 cluster). Slice 4 ships the **contract + stub verifier** raising `AutonomousMutationRejected("autonomous device mutation rejected: HITL approval token required")` on missing/invalid token.
- **Rollback timeout**: `Settings.nora_hitl_rollback_timeout_seconds` (default 300s); post-AP-change watchdog confirms management reachability; loss → revert.
- **Order**: migrate `ONLINE_ACTIVE` subscribers first (make-before-break); AP moves last; rollback on watchdog fire.
- **Exclusion**: `PRE_EXISTING_OFFLINE` subscribers NEVER targeted (no active session → false-timeout).
- **Sanitizer**: every response field passes `Sanitizer.sanitize(...)`.

## Zero-Leakage Plan

All credentials stay `pydantic.SecretStr`; `DeviceResolver` reads from `Settings` only. Tool responses pass through `Sanitizer`; `target_ip`/`host` redacted via R10 (`community|auth_password|priv_password`). No IP/hostname/serial/credential literals in any artifact; AST-guard via existing `tests/test_no_llm_journal_imports.py`. `DeviceDriverInterface` is the seam; vendor detail stays behind it (extends `tests/test_driver_airgap.py` AST).

## Approach

`DeviceDriverInterface(Protocol)` declares 6 methods; `Pmp450iSnmpDriver(Pmp450iDriver, DeviceDriverInterface)` adapts the existing driver with `report_firmware()` and routes `fetch_*` to per-cluster modules. `DeviceResolver.build(host, snmp_version, creds)` returns a frozen `Device` with collision-safe stem `f"adhoc-{host}-{secrets.token_hex(3)}"` — never mutates `devices.yaml`. Slices 2-4 add signed OIDs (via `scripts/sign_catalog.py`), a thin driver module, two `@mcp.tool` delegates, and a test file each. Slice 4 adds `hitl/tokens.py` + `threading.Timer` watchdog. Slice 5 ships E2E test + registration guard.

## Affected Areas

NEW: `drivers/{interface,resolver}.py`, `snmp_pmp450i/{summaries,subscribers,spectrum,migrate}.py`, `hitl/tokens.py`. MODIFIED: `drivers/snmp_pmp450i/driver.py`, `registry.py` (slice 1), `server.py` (slices 2-5: 6 tools + guard), `config.py` (slice 4 additive `nora_hitl_*`), `data/oid-catalogs/cambium/pmp450i/*.json` (re-sign each slice). NEW tests: `test_{driver_interface,resolver,snmp_summaries,snmp_subscribers,snmp_spectrum,snmp_migrate,hitl_tokens,oid_catalog_integration}.py`. No edits to `intervention_memory` or `intervention_writer`.

## Risks

| Risk | Sev | Mitigation |
|------|-----|------------|
| **Slice 4** bypasses HITL via direct MCP call or stub mis-design | Critical | Stub ALWAYS raises on missing/invalid token; `migrate_autonomous_call_raises_autonomous_mutation_rejected`; pair-review mandatory. |
| `DeviceResolver` leaks credentials | High | All creds `SecretStr`; `secret_str_safety_no_plaintext_in_repr`; AST extended. |
| Slice 3 `PRE_EXISTING_OFFLINE` boundary drifts | High | Exclusion in ONE `categorize_subscribers(...)` returning `Literal["ONLINE_ACTIVE","ACTIVE_DEGRADED","PRE_EXISTING_OFFLINE"]`; `get_intervention_history_called_before_categorize` cross-checks. |
| Catalog minor-mismatch silent for non-driver tools | Med | New modules call `OidCatalogRegistry.resolve(...)` first; same telemetry channel; slice 5 E2E covers it. |
| 5-PR chain stuck mid-flight | Med | Each PR lands green (`verify.test_command` + ruff + mypy `--strict`); chain halts on first red. |

## Rollback Plan

- **Slice 1** revert: drop `drivers/{interface,resolver}.py`; restore `registry.py` to single-vendor shape. No on-disk state.
- **Slices 2-3** revert: remove 4 `@mcp.tool`s; drop per-cluster driver modules + tests; re-sign original 15-OID seed.
- **Slice 4** revert: remove 2 `@mcp.tool`s; drop `hitl/tokens.py` + `nora_hitl_*` Settings. **Test-only against `snmpsim`** — no real-equipment state. Backout: `nora_hitl_token_ttl_seconds=0` makes stub reject every token.
- **Slice 5** revert: delete `test_oid_catalog_integration.py` + registration guard. #24 stays open.
- **Git**: single PR revert each; no schema migration — `devices.yaml`, catalog JSONs (HMAC-pinned), intervention JSONs unchanged.

## Dependencies

**Internal**: `nora.drivers.inventory.Device` (frozen), `Pmp450iDriver` (slice 1), `OidCatalogRegistry.resolve` (slices 2-4), `nora.config.Settings` (slice 4), `save_intervention_record` (slice 4 emits on success), `search_intervention_history` (slice 3 reads `known_pre_existing_offline_subscribers`). **Stdlib**: `secrets`, `hashlib`, `threading` (slice 4 watchdog). No new runtime deps. **Dev**: `snmpsim` covers slices 1-2; slice 3 needs synthetic subscriber-table fixtures; slice 4 needs `snmpsim` SET simulator.

## Success Criteria

- [ ] All 5 PRs merge green; `uv run python -m pytest --cov=src/nora --cov-report=term-missing` exits 0 with coverage ≥ 85%.
- [ ] `uv run ruff check . && uv run ruff format --check . && uv run mypy --strict src/nora` exits 0 across the chain.
- [ ] PR 5 carries `Closes #14, Closes #15, Closes #24`.
- [ ] All 26 named tests pass in CI.
- [ ] Slice 1 keeps `Pmp450iDriver` 100% back-compatible: `test_driver_snmp_pmp450i`, `test_driver_snmpsim_{v2c,v3}`, `test_driver_snmp450i_readonly`, `test_driver_airgap` stay green.
- [ ] Slice 5 proves ADR #17 Tests 9 + 10 E2E in CI.
- [ ] Each PR ≤ 800 lines.
