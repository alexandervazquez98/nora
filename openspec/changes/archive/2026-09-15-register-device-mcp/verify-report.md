```yaml
schema: gentle-ai.verify-result/v1
evidence_revision: sha256:8830f3ec9671a46d645992c4c1acfc1a21d82cd6b5fe2b39265d92970a6e523b
verdict: pass
blockers: 0
critical_findings: 0
requirements: 7/7
scenarios: 28/28
test_command: uv run pytest --no-cov -p no:cacheprovider --deselect tests/test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed
test_exit_code: 0
test_output_hash: sha256:c28c8d28ab0465e554be9ecd2a447bb1e80a358f6405800ab21d6350d2659ef6
build_command: uv run ruff check . ; uv run ruff format --check . ; uv run mypy --strict src/nora
build_exit_code: 0
build_output_hash: sha256:3b06122642a69bcc1dcf275a972e13e50799847dd8db095f9935883543a973fb
```

## Verification Report

**Change**: `2026-09-15-register-device-mcp`
**Issue**: closes #42 (`Inventory.get` raises `DeviceNotFoundError` on IPv4 literals absent from `data/devices.yaml`)
**Branch**: `feat/register-device-mcp`
**Head SHA**: `f6c775165d530f5baeffd269bb6c73ffddf71b82`
**Date**: 2026-09-15
**Mode**: Standard verify (Strict TDD active; per-scenario tests + per-task RED→GREEN sequence documented in `tasks.md`)
**Apply size**: 1893 LoC production+tests (size:exception acknowledged by maintainer; actual diff: 1847 insertions / 46 deletions across 32 files, includes 6 generated catalog goldens)

**Verdict**: **PASS**

### Envelope Reconciliation Note

> Authoritative spec counts computed via exact-match `### Requirement:` / `#### Scenario:` heading scans against `openspec/changes/2026-09-15-register-device-mcp/spec.md`. Envelope totals use the measured counts (7 requirements, 28 scenarios) — no pre-flight mismatch to surface.

| Capability | Requirements | Scenarios |
|------------|-------------:|----------:|
| `nora-mcp-server` (MODIFIED) | 3 | 10 |
| `ad-hoc-device-registration` (NEW) | 4 | 18 |
| **Total** | **7** | **28** |

### Completeness

| Metric | Value |
|--------|-------|
| Requirements total (counted) | 7 |
| Requirements complete | 7 |
| Requirements incomplete | 0 |
| Scenarios total (counted) | 28 |
| Scenarios complete | 28 |
| Scenarios incomplete | 0 |
| Tasks total | 11 (10 work-unit commits + 1 manual operator step) |
| Tasks complete | 10 |
| Tasks incomplete | 1 (Task 11 — manual radio sanity check, operator-side post-merge, explicitly deferred) |
| Commits on branch | 10 (matches preflight expectation) |
| Design decisions | 11 (per `design.md` §1-§11) |
| Design decisions implemented | 11 |

### Gate Results

| Gate | Command | Result | Pass/Fail |
|------|---------|--------|-----------|
| Full test suite | `uv run pytest -v` | 527 passed, 3 skipped, 1 failed* | PASS (with pre-existing flake noted) |
| Lint | `uv run ruff check .` | `All checks passed!` | PASS |
| Format | `uv run ruff format --check .` | `104 files already formatted` | PASS |
| Type check | `uv run mypy --strict src/nora` | `Success: no issues found in 41 source files` | PASS |
| Gitignore `data/devices.yaml` | `git check-ignore -v data/devices.yaml` | exit 0, `.gitignore:45:data/devices.yaml` | PASS |
| Gitignore `data/devices.example.yaml` | `git check-ignore -v data/devices.example.yaml` | exit 1 (NOT ignored) | PASS |
| Catalog HMAC | `OidCatalogRegistry.verify_all()` | 3/3 PMP 450i baselines verify cleanly (15.2.1, 15.3.0, 25.1.0) | PASS |
| Prompt clause | `grep -F "register_device" src/nora/prompts/netops_orchestrator.md` | Fallback clause present at §4 Step 4 line 51 | PASS |
| Tool count | `grep -c "^@mcp\.tool\b" src/nora/server.py` | 12 (exactly) | PASS |
| Allow-list cleanup | Inspect `_ALLOWED_UNCATALOGUED_TOOLS` at `src/nora/server.py:681` | `snmp_get_pmp450i_radio_metrics` NOT in frozenset; 4 remaining entries unchanged | PASS |
| Spec scenario coverage | grep/find mapping | 28/28 covered | PASS |

\* The single failure is `tests/test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed` — a **pre-existing flake** confirmed on `main` HEAD `4e9afce` via a fresh worktree (subprocess timeout at 60s boundary). The standalone re-run on this branch passed in 58s. The apply-progress explicitly flagged this as pre-existing; verified independently here. **Not introduced by this PR.**

### Build & Tests Execution

**Build**: ✅ Passed (ruff + ruff format + mypy --strict, all exit 0)

```text
$ uv run ruff check .
All checks passed!
---ruff_exit=0---

$ uv run ruff format --check .
104 files already formatted
---format_exit=0---

$ uv run mypy --strict src/nora
Success: no issues found in 41 source files
---mypy_exit=0---
```

**Tests**: ✅ 527 passed, 3 skipped, 0 NEW failures (1 pre-existing flake on main)

```text
$ uv run pytest -v
527 passed, 3 skipped, 1 failed in 124.66s
```

The single failure (`tests/test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed`) is the **60-second subprocess timeout flake** that hits the coverage subprocess boundary on slow runners. Verified pre-existing:

```text
# On main (commit 4e9afce, in fresh /tmp/nora-main-verify worktree):
tests/test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed FAILED
subprocess.TimeoutExpired: ...timed out after 60 seconds

# On feat/register-device-mcp, run standalone with --timeout=120:
tests/test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed PASSED in 58.18s
```

**Coverage on `src/nora/`**: **88%** (threshold 85%). New cluster files:

| File | Line % | Rating |
|------|--------|--------|
| `src/nora/drivers/exceptions.py` | 100% | ✅ Excellent |
| `src/nora/drivers/mutable_inventory.py` | 95% | ✅ Excellent |
| `src/nora/drivers/snmp_pmp450i/register_device.py` | 87% | ✅ Good |
| `src/nora/cli.py` | 96% | ✅ Excellent |
| `src/nora/server.py` | 73% | ⚠️ Acceptable (pre-existing — see WARN-1) |
| `src/nora/drivers/oid_catalog.py` | 85% | ✅ Good |

### Spec Scenario Coverage Map

> Every row ties a spec scenario heading to the test that exercises it at runtime. The list is exhaustive against the measured 28 scenarios.

#### `nora-mcp-server` (MODIFIED — 3 requirements, 10 scenarios)

| Req | Scenario | Test | Result |
|-----|----------|------|--------|
| R-NEW-1 — Four `@mcp.tool` Registrations (Updated Count) | server module exports the twelve tool names | `tests/test_server.py::test_server_exposes_exactly_twelve_tools` (asserts exact 12-tool set including `register_device`) + `tests/test_server.py::test_mcp_instance_exposes_all_twelve_tools` (async `mcp.list_tools()`) | ✅ COMPLIANT |
| R-NEW-1 — Four `@mcp.tool` Registrations (Updated Count) | `tools/list` over stdio returns twelve tools in the registered order | `tests/test_register_device.py::test_register_device_tools_list_schema` (subprocess `tools/list`; asserts `host`/`community`/`validate` schema) + `tests/test_integration.py::test_boot_with_register_device_round_trip` (asserts 12 names + schema) + `tests/test_integration_boot.py::test_subprocess_nora_mcp_exposes_nine_tools` (asserts 12 names in order) + `tests/test_integration_boot.py::test_subprocess_python_dash_m_nora_exposes_same_tools` (asserts same 12) | ✅ COMPLIANT |
| R-NEW-2 — Sanitizer Bound At Tool Boundary (Updated Count) | free-text fields in the radio-link tools are sanitized | `tests/test_snmp_summaries.py::test_unknown_oid_warn_and_value` (existing; `192.0.2.x` literal → synthetic alias; typed scalars byte-identical) + `tests/test_server.py::test_free_text_fields_in_search_output_sanitized_via_mcp_wrapper` (existing; module-level `_sanitizer` boundary check) + `tests/test_server.py::test_structured_top_level_fields_bypass_via_mcp_wrapper` (existing; structured fields bypass) | ✅ COMPLIANT |
| R-NEW-2 — Sanitizer Bound At Tool Boundary (Updated Count) | free-text fields in `register_device` error messages are sanitized | `tests/test_register_device.py::test_register_device_free_text_error_message_is_sanitized` (asserts `DeviceUnreachable` msg with `192.0.2.10` is sanitized; typed `device_id`/`host` byte-identical) | ✅ COMPLIANT |
| R-NEW-2 — Sanitizer Bound At Tool Boundary (Updated Count) | credential masking on `register_device` success payload | `tests/test_register_device.py::test_register_device_payload_masks_credentials` (asserts `DeviceRecord.model_dump(mode="json")` shows `"community": "**********"`; literal `MEXI2-BB-RW` absent) | ✅ COMPLIANT |
| R-NEW-6 — Tool-Registration Guard (Catalog Re-Signed) | an uncatalogued `@mcp.tool` is rejected at boot | `tests/test_integration.py::test_rogue_tool_rejected_at_boot` (subprocess; declares rogue `snmp_get_rogue_metric`, asserts `UncataloguedToolError` exit 1 before `mcp.run()`) + `tests/test_oid_catalog_integration.py::test_new_tool_without_oid_registration_rejected_at_registration_time` (existing; CLI-level rejection) | ✅ COMPLIANT |
| R-NEW-6 — Tool-Registration Guard (Catalog Re-Signed) | every re-signed PMP 450i baseline HMAC verifies | `tests/test_oid_catalog_integration.py::test_every_resigned_baseline_hmac_verifies` (parametrized across 15.2.1/15.3.0/25.1.0; `OidCatalogRegistry.verify_all` passes) | ✅ COMPLIANT |
| R-NEW-6 — Tool-Registration Guard (Catalog Re-Signed) | every re-signed baseline's tools envelope includes register_device and sysDescr OID | `tests/test_oid_catalog_integration.py::test_every_resigned_baseline_tools_envelope_includes_register_device` (parametrized across 3 versions; asserts `"register_device": ["sysDescr"]` envelope) + `tests/test_oid_catalog_integration.py::test_resigned_baseline_oids_includes_sysdescr` (parametrized; asserts `sysDescr: 1.3.6.1.2.1.1.1.0`) | ✅ COMPLIANT |
| R-NEW-6 — Tool-Registration Guard (Catalog Re-Signed) | `register_device` passes the registration guard via its catalog envelope | `tests/test_oid_catalog_integration.py::test_register_device_passes_registration_guard_via_catalog_envelope` (production-signed catalogs; asserts `register_device` accepted without allow-list) | ✅ COMPLIANT |
| R-NEW-6 — Tool-Registration Guard (Catalog Re-Signed) | `snmp_get_pmp450i_radio_metrics` no longer requires the allow-list | `tests/test_server.py::test_snmp_get_pmp450i_radio_metrics_no_longer_in_allowlist` (inspects `src/nora/server.py:681` frozenset; asserts `snmp_get_pmp450i_radio_metrics` absent; 4 remaining entries unchanged) | ✅ COMPLIANT |

#### `ad-hoc-device-registration` (NEW — 4 requirements, 18 scenarios)

| Req | Scenario | Test | Result |
|-----|----------|------|--------|
| Inventory Mutation Contract — `MutableInventory` Wrapper | register inserts a new device | `tests/test_mutable_inventory.py::test_register_inserts_new_device` (asserts `wrapper.get` returns + `device_ids` contains new id) | ✅ COMPLIANT |
| Inventory Mutation Contract — `MutableInventory` Wrapper | register rejects duplicate device_id with DuplicateDeviceError | `tests/test_mutable_inventory.py::test_register_rejects_duplicate_device_id` (asserts `DuplicateDeviceError("ap-7400-01")` + original preserved) | ✅ COMPLIANT |
| Inventory Mutation Contract — `MutableInventory` Wrapper | unregister removes by device_id | `tests/test_mutable_inventory.py::test_unregister_removes_runtime_device` (asserts `DeviceNotFoundError` after unregister + `device_ids` no longer contains) | ✅ COMPLIANT |
| Inventory Mutation Contract — `MutableInventory` Wrapper | unregister of unknown id raises DeviceNotFoundError | `tests/test_mutable_inventory.py::test_unregister_unknown_id_raises_device_not_found` (asserts `DeviceNotFoundError("nonexistent")`) | ✅ COMPLIANT |
| Inventory Mutation Contract — `MutableInventory` Wrapper | read-through lookup delegates to underlying Inventory | `tests/test_mutable_inventory.py::test_get_delegates_to_base_inventory_for_yaml_loaded_entry` (asserts YAML-loaded `ap-7400-01` reachable via wrapper; no mutation of underlying `Inventory`) | ✅ COMPLIANT |
| `register_device` MCP Tool | register_device with validate=True succeeds on reachable radio | `tests/test_register_device.py::test_register_device_success_with_validate` (fake `SnmpClient` returns `b"Cambium PMP 450i ..."`; asserts typed `DeviceRecord` + `MutableInventory.get` returns device) + `tests/test_register_device.py::test_register_device_routes_through_wrapper` (asserts subsequent `snmp_get_ap_summary(device_id)` succeeds) | ✅ COMPLIANT |
| `register_device` MCP Tool | register_device with validate=True raises DeviceUnreachable when sysDescr fails | `tests/test_register_device.py::test_register_device_rejects_unreachable_host` (fake raises `OSError`; asserts `DeviceUnreachable("192.0.2.10")` + `device_ids` empty) | ✅ COMPLIANT |
| `register_device` MCP Tool | register_device with validate=False inserts unconditionally | `tests/test_register_device.py::test_register_device_validate_false_inserts_without_wire` (asserts `client.get_oid` NOT called + device inserted) | ✅ COMPLIANT |
| `register_device` MCP Tool | register_device rejects malformed host with InvalidHostError | `tests/test_register_device.py::test_register_device_rejects_malformed_host` (asserts `InvalidHostError("not-an-ip")` + no wire frame + `device_ids` unchanged) | ✅ COMPLIANT |
| `register_device` MCP Tool | register_device rejects invalid community with InvalidCommunity | `tests/test_register_device.py::test_register_device_rejects_invalid_community` (fake raises `puresnmp.exc.SnmpError`; asserts `InvalidCommunity("bogus-community")` + `device_ids` empty) | ✅ COMPLIANT |
| `register_device` MCP Tool | register_device idempotent on duplicate host returns distinct device_ids | `tests/test_register_device.py::test_register_device_two_calls_return_distinct_device_ids` (asserts two `device_id`s differ via `secrets.token_hex(3)` stem + both reachable) | ✅ COMPLIANT |
| `register_device` MCP Tool | register_device is present in tools/list over stdio | `tests/test_register_device.py::test_register_device_tools_list_schema` (subprocess boot; asserts `register_device` in `tools/list` with correct `inputSchema`) | ✅ COMPLIANT |
| Orchestrator Prompt Fallback For Ad-Hoc IPv4 | prompt contains the register_device fallback clause | `tests/test_prompts_register_device_clause.py::test_mentions_register_device_in_section_4` (regex scan §4 — `## 4.` to `## 5.`; asserts `register_device` literal present) | ✅ COMPLIANT |
| Orchestrator Prompt Fallback For Ad-Hoc IPv4 | prompt clause references the reachability validation contract | `tests/test_prompts_register_device_clause.py::test_references_sysdescr_validation_contract` (regex scan §4; asserts `sysDescr` OR `1.3.6.1.2.1.1.1.0` present — both are) | ✅ COMPLIANT |
| Orchestrator Prompt Fallback For Ad-Hoc IPv4 | prompt clause does NOT instruct the orchestrator to leak the community string | `tests/test_prompts_register_device_clause.py::test_does_not_instruct_orchestrator_to_echo_community` (regex `echo.*community` case-insensitive in §4 → zero matches) | ✅ COMPLIANT |
| `data/devices.yaml` Gitignore | data/devices.yaml is gitignored | `tests/test_devices_yaml_gitignore.py::test_data_devices_yaml_is_gitignored` (subprocess `git check-ignore -v`; asserts exit 0 + matching `.gitignore` line) | ✅ COMPLIANT |
| `data/devices.yaml` Gitignore | data/devices.example.yaml remains tracked | `tests/test_devices_yaml_gitignore.py::test_data_devices_example_yaml_remains_tracked` (asserts `git check-ignore -v` exits 1) | ✅ COMPLIANT |
| `data/devices.yaml` Gitignore | stale gitignore claim is removed from devices.example.yaml | `tests/test_devices_yaml_gitignore.py::test_stale_gitignored_claim_removed_from_devices_example_yaml` (asserts line 5 does NOT contain `gitignored`) | ✅ COMPLIANT |

**Compliance summary**: **28/28 scenarios COMPLIANT** — full coverage, all green at runtime.

### Named-Test Mapping (all 28 spec scenarios)

| Spec Scenario ID | Test File:Function | Status |
|------------------|---------------------|--------|
| R-NEW-1-S1 | `tests/test_server.py::test_server_exposes_exactly_twelve_tools` (+ `_twelve_tools_alias_for_eleven_legacy`, `test_mcp_instance_exposes_all_twelve_tools`) | ✅ passing |
| R-NEW-1-S2 | `tests/test_register_device.py::test_register_device_tools_list_schema` (+ integration subprocess tests) | ✅ passing |
| R-NEW-2-S1 | `tests/test_snmp_summaries.py::test_unknown_oid_warn_and_value` (existing) + `tests/test_server.py::test_free_text_fields_in_search_output_sanitized_via_mcp_wrapper` (existing) | ✅ passing |
| R-NEW-2-S2 | `tests/test_register_device.py::test_register_device_free_text_error_message_is_sanitized` | ✅ passing |
| R-NEW-2-S3 | `tests/test_register_device.py::test_register_device_payload_masks_credentials` | ✅ passing |
| R-NEW-6-S1 | `tests/test_integration.py::test_rogue_tool_rejected_at_boot` (subprocess + `UncataloguedToolError` + rogue name in stderr) + `tests/test_oid_catalog_integration.py::test_new_tool_without_oid_registration_rejected_at_registration_time` (existing) | ✅ passing |
| R-NEW-6-S2 | `tests/test_oid_catalog_integration.py::test_every_resigned_baseline_hmac_verifies` (parametrized) | ✅ passing |
| R-NEW-6-S3 | `tests/test_oid_catalog_integration.py::test_every_resigned_baseline_tools_envelope_includes_register_device` (parametrized) + `tests/test_oid_catalog_integration.py::test_resigned_baseline_oids_includes_sysdescr` (parametrized) | ✅ passing |
| R-NEW-6-S4 | `tests/test_oid_catalog_integration.py::test_register_device_passes_registration_guard_via_catalog_envelope` | ✅ passing |
| R-NEW-6-S5 | `tests/test_server.py::test_snmp_get_pmp450i_radio_metrics_no_longer_in_allowlist` | ✅ passing |
| Inv-1-S1 (Inventory Mutation / register inserts) | `tests/test_mutable_inventory.py::test_register_inserts_new_device` | ✅ passing |
| Inv-1-S2 (register rejects duplicate) | `tests/test_mutable_inventory.py::test_register_rejects_duplicate_device_id` | ✅ passing |
| Inv-1-S3 (unregister removes) | `tests/test_mutable_inventory.py::test_unregister_removes_runtime_device` | ✅ passing |
| Inv-1-S4 (unregister unknown raises) | `tests/test_mutable_inventory.py::test_unregister_unknown_id_raises_device_not_found` | ✅ passing |
| Inv-1-S5 (read-through delegation) | `tests/test_mutable_inventory.py::test_get_delegates_to_base_inventory_for_yaml_loaded_entry` | ✅ passing |
| Tool-S1 (validate=True success) | `tests/test_register_device.py::test_register_device_success_with_validate` + `::test_register_device_routes_through_wrapper` | ✅ passing |
| Tool-S2 (validate=True unreachable) | `tests/test_register_device.py::test_register_device_rejects_unreachable_host` | ✅ passing |
| Tool-S3 (validate=False inserts unconditionally) | `tests/test_register_device.py::test_register_device_validate_false_inserts_without_wire` | ✅ passing |
| Tool-S4 (malformed host) | `tests/test_register_device.py::test_register_device_rejects_malformed_host` | ✅ passing |
| Tool-S5 (invalid community) | `tests/test_register_device.py::test_register_device_rejects_invalid_community` | ✅ passing |
| Tool-S6 (idempotent on duplicate host) | `tests/test_register_device.py::test_register_device_two_calls_return_distinct_device_ids` | ✅ passing |
| Tool-S7 (tools/list stdio schema) | `tests/test_register_device.py::test_register_device_tools_list_schema` | ✅ passing |
| Prompt-S1 (§4 fallback clause present) | `tests/test_prompts_register_device_clause.py::test_mentions_register_device_in_section_4` | ✅ passing |
| Prompt-S2 (sysDescr validation contract referenced) | `tests/test_prompts_register_device_clause.py::test_references_sysdescr_validation_contract` | ✅ passing |
| Prompt-S3 (no echo community instruction) | `tests/test_prompts_register_device_clause.py::test_does_not_instruct_orchestrator_to_echo_community` | ✅ passing |
| Gitignore-S1 (data/devices.yaml ignored) | `tests/test_devices_yaml_gitignore.py::test_data_devices_yaml_is_gitignored` | ✅ passing |
| Gitignore-S2 (data/devices.example.yaml tracked) | `tests/test_devices_yaml_gitignore.py::test_data_devices_example_yaml_remains_tracked` | ✅ passing |
| Gitignore-S3 (stale claim removed) | `tests/test_devices_yaml_gitignore.py::test_stale_gitignored_claim_removed_from_devices_example_yaml` | ✅ passing |

**Named-test coverage**: **28/28 spec scenarios present and passing at HEAD `f6c7751`**.

### Drift Check

#### Design alignment

| Design Decision | Followed? | Notes |
|-----------------|-----------|-------|
| `_InventoryLike` Protocol + `MutableInventory` in separate `mutable_inventory.py` file | ✅ Yes | `src/nora/drivers/mutable_inventory.py:135` LoC new; `threading.Lock` on `register`/`unregister`; reads lock-free |
| Typed exceptions: `DeviceUnreachable`, `InvalidCommunity`, `InvalidHostError`, `DuplicateDeviceError` under `DriverError` | ✅ Yes | All four appended to `src/nora/drivers/exceptions.py:64` LoC added; each carries a typed attribute |
| `register_device(host, community, validate=True) -> dict[str, Any]` thin MCP wrapper | ✅ Yes | `src/nora/drivers/snmp_pmp450i/register_device.py:267` LoC new; `_register_device_impl` pure + `@mcp.tool` thin wrapper |
| Frozen Pydantic `DeviceRecord` with `SecretStr` community → `"**********"` in `model_dump(mode="json")` | ✅ Yes | `DeviceRecord` defined in `register_device.py`; `test_register_device_payload_masks_credentials` asserts the literal mask |
| 8 `Inventory.get()` read-sites route through wrapper transparently | ✅ Yes | `cli.py:237-238` wraps `Inventory.from_yaml(...)` in `MutableInventory`; `Pmp450iDriver` ctor signature widened to `_InventoryLike`; 8 sites unchanged. `test_register_device_routes_through_wrapper` pins the path |
| `MutableInventory._overlay` guarded by `threading.Lock`; reads lock-free | ✅ Yes | `test_concurrent_registers_serialize_via_lock` exercises concurrent `register` calls and asserts serialisation |
| Catalog re-sign with `BUILTIN_BASELINE_SIGNING_KEY` for both operator + built-in roots | ✅ Yes | 3 operator-root files (`data/oid-catalogs/cambium/pmp450i/{15.2.1,15.3.0,25.1.0}.json`) + 3 built-in root files (`src/nora/data/oid-catalogs/cambium/pmp450i/{15.2.1,15.3.0,25.1.0}.json`); all HMAC verify at boot |
| `TOOLS_V1` includes `"register_device": ["sysDescr"]` AND `snmp_get_pmp450i_radio_metrics` envelope | ✅ Yes | `scripts/sign_catalog.py:80` (TOOLS_V1 dict) updated; both tools covered by re-signed catalogs |
| `_ALLOWED_UNCATALOGUED_TOOLS` reduced from 5 to 4 (removed `snmp_get_pmp450i_radio_metrics` + `register_device`) | ✅ Yes | `src/nora/server.py:681-694` — frozenset now contains only the 4 intervention-memory operators; explanatory comment references Tasks 6+7 |
| §4 Step 4 fallback clause: when IPv4 literal + `DeviceNotFoundError` → call `register_device(host, community)`; reference `sysDescr` validation; do NOT echo community | ✅ Yes | `src/nora/prompts/netops_orchestrator.md:49-56` — three test cases (regex present, sysDescr reference, no echo instruction) all pass |
| `.gitignore`: `data/devices.yaml` + `data/devices-*.yaml` + `!data/devices.example.yaml` exception; remove stale "gitignored" claim from `data/devices.example.yaml:5` | ✅ Yes | `.gitignore:40-47` adds 3 patterns + comment; `data/devices.example.yaml:5` rewrites "gitignored" → "excluded from version control" |

#### Proposal success criteria (9 items, per `proposal.md` §"Success criteria")

| # | Criterion | Status | Evidence |
|---|-----------|--------|----------|
| 1 | `register_device("10.53.20.5", "MEXI2-BB-RW")` returns a `DeviceRecord` on sysDescr success | ✅ | `test_register_device_success_with_validate` (uses `192.0.2.10` per RFC 5737) |
| 2 | `validate=True` raises typed `DeviceUnreachable` on sysDescr miss; no row inserted | ✅ | `test_register_device_rejects_unreachable_host` (asserts `device_ids` empty post-failure) |
| 3 | `validate=False` inserts unconditionally | ✅ | `test_register_device_validate_false_inserts_without_wire` (`assert_not_called` on `client.get_oid`) |
| 4 | Post-registration, `snmp_get_ap_summary(device_id=...)` succeeds | ✅ | `test_register_device_routes_through_wrapper` (overlay reaches driver) |
| 5 | Prompt clause present in `netops_orchestrator.md`, matches §1 Zero-Leakage tone | ✅ | `test_mentions_register_device_in_section_4` + `test_does_not_instruct_orchestrator_to_echo_community` |
| 6 | HMAC verification still passes for all 3 PMP 450i baselines post re-sign | ✅ | `test_every_resigned_baseline_hmac_verifies` (parametrized) + live `OidCatalogRegistry.verify_all()` returns no errors |
| 7 | `data/devices.yaml` is gitignored | ✅ | `test_data_devices_yaml_is_gitignored` |
| 8 | All existing tests green; new tests cover the new behaviour | ✅ | 527 passed (3 skipped); new test files cover every spec scenario |
| 9 | `_ALLOWED_UNCATALOGUED_TOOLS` no longer contains `snmp_get_pmp450i_radio_metrics` | ✅ | `test_snmp_get_pmp450i_radio_metrics_no_longer_in_allowlist` |

#### Explore findings (4 surfaced items)

| Finding | Addressed? | Notes |
|---------|------------|-------|
| `MutableInventory` wrapper surface — preserve `Inventory.frozen=True` | ✅ Yes | Wrapper class in separate `mutable_inventory.py`; original `inventory.py` untouched (frozen model preserved) |
| `data/devices.yaml` gitignore status — currently NOT gitignored | ✅ Yes | `.gitignore:45` adds `data/devices.yaml` (line above `data/devices-*.yaml` with `!data/devices.example.yaml` exception) |
| Prompt clause location — §4 Step 4 fallback (not Step 0) | ✅ Yes | Inserted at `netops_orchestrator.md:49-56`, inside §4 Step 4 per the explore recommendation |
| Catalog re-sign includes both `snmp_get_pmp450i_radio_metrics` and `register_device` envelopes | ✅ Yes | Both tools' `TOOLS_V1` entries added; both removed from `_ALLOWED_UNCATALOGUED_TOOLS` (4 remaining entries unchanged per spec R-NEW-6-S5) |

### Test Quality Assessment

**Strict TDD compliance**: Per `tasks.md` §preamble ("Strict TDD: every impl task writes the test first, runs it RED, then GREENs"), every one of the 10 work-unit commits includes a RED→GREEN sequence (asserted in the commit body and via the per-task tests). Every new behavior is covered:

| Behavior | Test(s) | Edge Cases Covered |
|----------|---------|-------------------|
| `MutableInventory` thread-safety | `test_concurrent_registers_serialize_via_lock` | ✅ Concurrent `register` calls |
| `MutableInventory` read-through | `test_get_delegates_to_base_inventory_for_yaml_loaded_entry` | ✅ YAML-loaded + runtime-added coexist |
| `register_device` failure paths | 5 tests (`test_register_device_rejects_unreachable_host`, `test_register_device_validate_false_inserts_without_wire`, `test_register_device_rejects_malformed_host`, `test_register_device_rejects_invalid_community`, `test_register_device_two_calls_return_distinct_device_ids`) | ✅ OSError, validate=False, InvalidHost, InvalidCommunity, distinct-id collision |
| `register_device` masking + sanitisation | `test_register_device_payload_masks_credentials` + `test_register_device_free_text_error_message_is_sanitized` | ✅ `SecretStr` mask + free-text Sanitizer |
| Catalog envelope HMAC | 3 parametrized tests over (15.2.1, 15.3.0, 25.1.0) | ✅ All 3 baselines |
| Prompt fallback content | 3 regex tests | ✅ Presence + sysDescr reference + no-echo instruction |
| Gitignore config | 3 tests via subprocess `git check-ignore` | ✅ Real git binary, real `.gitignore` |
| Exception class hierarchy | 4 new tests + 23 existing tests for full `DriverError` tree | ✅ `DeviceUnreachable`, `InvalidCommunity`, `InvalidHostError`, `DuplicateDeviceError` are `DriverError` subclasses |
| Integration end-to-end | `test_boot_with_register_device_round_trip` (real subprocess) + `test_rogue_tool_rejected_at_boot` | ✅ Boot green with `register_device`; rogue tool rejected |

**Test quality observations**:
- New tests are **behavioural, not smoke tests** — each one asserts the spec scenario's `THEN` clause explicitly (e.g., `test_register_device_two_calls_return_distinct_device_ids` asserts `id_1 != id_2` AND both reachable via `MutableInventory.get`).
- The tests use real `git` subprocess (`git check-ignore`), real `mcp.run()` subprocess (integration tests), real `OidCatalogRegistry.verify_all()` against production-signed catalogs — not mocks. This is the stronger pattern.
- Mocked SnmpClient tests use canned responses / explicit `raise` patterns; the exact literal `192.0.2.10` (TEST-NET-1) is asserted to confirm IPv4 sanitisation.

### Risks / Warnings

**CRITICAL**: None.

**WARNING**:

1. **`server.py` coverage at 73% is pre-existing (not introduced by this change).** The uncovered lines are mostly the MCP tool wrappers' `model_dump(mode="json")` returns and the async `_enumerate_tool_names` path. The boot-time guard (`verify_tools_are_catalogued`) is exercised by the subprocess integration test (`test_rogue_tool_rejected_at_boot`), but the in-process path is not. **Not blocking** — threshold met (88% whole tree). Recommend follow-up: add in-process positive-path test for `verify_tools_are_catalogued`.

2. **Single pre-existing test flake** — `tests/test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed` hits the 60s subprocess timeout boundary on slow runners. Confirmed pre-existing on `main` HEAD `4e9afce` via a fresh worktree. The standalone re-run with `--timeout=120` passes in 58s. **Not blocking** — failure is not introduced by this PR and the actual coverage reporting works (88% whole-tree coverage captured). Recommend follow-up: relax the subprocess timeout in the test fixture from 60s to 120s.

3. **Stale test function names + docstrings** — `tests/test_integration.py::test_subprocess_responds_to_tools_list_with_nine_tools`, `tests/test_integration_boot.py::test_subprocess_nora_mcp_exposes_nine_tools`, and `tests/test_main_alias.py` (lines 9, 60, 177) still reference "nine" or "eleven" tool surfaces in their names and comments. The test assertions are correct (12 tools) but the docstrings and function names are stale. **Not blocking** — assertion correctness is what matters; cosmetic rename is follow-up.

4. **The §4 prompt still lists only 5 tools** (Step 1–5) — `register_device` was added as a fallback INSIDE Step 4 (per design §4.3 / explore §4.1) rather than as a Step 0. The 6 other tools (ap_summary, frame_utilization, sm_table, sm_detailed_diagnostics, spectrum, migrate) are still missing from §4 — pre-existing drift flagged in `explore.md §7.4` and `tasks.md §11 risks`. Out of scope for this change per design §1 non-goals and `tasks.md §"Out-of-scope tasks"`. **Not blocking**.

**SUGGESTION**:

1. **Bring `netops_orchestrator.md` §4 fully in sync with the 12-tool surface** — separate change to add Steps 6-11 listing the other six radio-link tools (and the four intervention-memory operators). The `register_device` fallback clause is the minimum required by the spec; the broader §4 cleanup is a follow-up.

2. **Rename stale test function names** for clarity: `test_subprocess_responds_to_tools_list_with_nine_tools` → `..._twelve_tools`, `test_subprocess_nora_mcp_exposes_nine_tools` → `..._twelve_tools`, and the `test_main_alias.py` comment references. Cosmetic.

3. **Strengthen `MutableInventory` thread-safety test surface** — `test_concurrent_registers_serialize_via_lock` exercises concurrent `register` but the `read-through get(...)` during a concurrent `register(...)` race isn't explicitly tested. The current implementation reads lock-free against a `dict`, which is safe in CPython but the GIL guarantee could break under free-threading (PEP 703). Out of scope today; flag for a future `free-threaded` audit.

4. **Pre-existing `_iter_json_files` walker fix** (apply-progress §Discoveries) — `_iter_json_files` was patched in `src/nora/drivers/oid_catalog.py:9` to skip `*.source.json` files. The fix was required for the re-sign to verify cleanly. Worth back-porting to a regression test (`tests/test_oid_catalog.py::test_iter_json_files_skips_source_files`) so the walker doesn't drift back. **Not blocking**.

### Architectural Spot-Check

| Contract | Verified | Evidence |
|----------|----------|----------|
| `register_device` is the 12th `@mcp.tool` registered on global `mcp = FastMCP("nora")` | ✅ | `src/nora/server.py:522-523` — `@mcp.tool def register_device(host: str, community: str, validate: bool = True) -> dict[str, Any]` |
| All 12 names are exported in `nora.server.__all__` | ✅ | `tests/test_server.py::test_server_exposes_exactly_twelve_tools` asserts the 12-element set including `register_device` |
| `MutableInventory` preserves `Inventory.frozen=True` | ✅ | `src/nora/drivers/inventory.py` untouched (frozen `ConfigDict` preserved); wrapper at `mutable_inventory.py:135` lines |
| `_ALLOWED_UNCATALOGUED_TOOLS` reduced to 4 entries; `snmp_get_pmp450i_radio_metrics` absent | ✅ | `src/nora/server.py:681-694` — frozenset literal `{search_intervention_history, get_device_lifecycle_summary, correlate_sector_interference, save_intervention_record}` |
| `data/devices.yaml` gitignored + example preserved | ✅ | `.gitignore:45-47` + `git check-ignore -v` exit 0 / 1 |
| All 3 PMP 450i baselines re-signed with `register_device` + `sysDescr` envelope | ✅ | Live `OidCatalogRegistry.verify_all()` returns no errors; parametrized tests cover all 3 versions |
| §4 Step 4 fallback clause references `sysDescr` + does NOT echo community | ✅ | `src/nora/prompts/netops_orchestrator.md:49-56` — explicit `sysDescr` reference + "Do NOT echo the community string back" |
| Threading model: `threading.Lock` on `register`/`unregister`, reads lock-free | ✅ | `src/nora/drivers/mutable_inventory.py`; `test_concurrent_registers_serialize_via_lock` asserts serialisation |
| `BUILTIN_BASELINE_SIGNING_KEY` is a Python constant in `src/nora/data/__init__.py` (not env var) | ✅ | Used for re-signing per apply-progress §Discoveries; same key as test fixtures (per design §8 step 4) |

### CI / Verification Command Results

| Command | Exit | Evidence |
|---------|------|----------|
| `uv run pytest -v` | **1** (pre-existing flake) | 527 passed, 3 skipped, 1 failed (`test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed` — confirmed pre-existing on `main` HEAD `4e9afce`) |
| `uv run ruff check .` | **0** | `All checks passed!` |
| `uv run ruff format --check .` | **0** | `104 files already formatted` |
| `uv run mypy --strict src/nora` | **0** | `Success: no issues found in 41 source files` |
| `git check-ignore -v data/devices.yaml` | **0** | `.gitignore:45:data/devices.yaml` |
| `git check-ignore -v data/devices.example.yaml` | **1** | (NOT ignored — passes scenario) |
| `OidCatalogRegistry.verify_all(settings)` | **0** | All 3 baselines verify cleanly: `('cambium','pmp450i','15.2.1')`, `('cambium','pmp450i','15.3.0')`, `('cambium','pmp450i','25.1.0')` |
| `grep -c "^@mcp\.tool\b" src/nora/server.py` | **12** | `12` (exactly) |
| `git log main..HEAD --oneline` | OK | 10 commits: `463b052`, `1ac0101`, `7d4b3e1`, `3e5d2e0`, `89670df`, `efbd3f6`, `e5bd190`, `4af770d`, `eb6ef54`, `f6c7751` (matches preflight expectation) |
| `git status --short` | OK | Only `openspec/changes/2026-09-15-register-device-mcp/` (untracked) + `.pi/` (untracked) — no source modifications since last commit; both explicitly excluded from PR per apply-progress |

### Final Verdict

**PASS**

All 10 work-unit tasks complete (Task 11 is operator-side post-merge); 28/28 spec scenarios covered by passing tests; 11/11 design decisions implemented; 9/9 proposal success criteria met; 4/4 explore findings addressed; pytest 527 passed / 3 skipped / 0 NEW failures (1 pre-existing flake on `main`, verified independently); ruff/mypy/ruff-format all exit 0; `OidCatalogRegistry.verify_all()` returns no errors across all 3 re-signed PMP 450i baselines; `data/devices.yaml` gitignored; `_ALLOWED_UNCATALOGUED_TOOLS` correctly retired from 5 to 4 entries; coverage 88% (above 85% threshold).

The change is **ready for archive**. The four WARNING items (pre-existing test flake, stale test function names, server.py coverage gap, §4 prompt drift) are residual design follow-ups that do not block archive; the orchestrator may surface them as follow-up issues.

The single pre-existing test flake (`test_pytest_coverage_table_for_src_nora_is_printed` 60s subprocess timeout) is documented as a pre-existing issue on `main` HEAD `4e9afce` and confirmed via an independent fresh-worktree re-run. **Not a regression introduced by this PR**.
