# Design: register_device MCP Tool + Orchestrator Prompt Update

Closes #42. Single PR, ~280 LoC production + ~140 LoC tests.

## 1. Goals and non-goals

**Goals.** `@mcp.tool register_device(host, community, validate=True) -> DeviceRecord`. `MutableInventory` preserving `Inventory.frozen=True`. Orchestrator §4 Step 4 fallback. Re-sign PMP 450i catalogs (15.2.1/15.3.0/25.1.0). Gitignore `data/devices.yaml`. Retire `_ALLOWED_UNCATALOGUED_TOOLS` entry for `snmp_get_pmp450i_radio_metrics`.

**Non-goals.** YAML persistence; hot-reload; discovery; firmware pinning; batch import; GUI; `NORA_DEFAULT_SNMP_COMMUNITY`; `community` on existing tools; bringing §4 prompt fully in sync with 12 tools.

## 2. Architecture overview

```
operator → MCP client → FastMCP("nora")
                       @mcp.tool register_device
                          │
                ┌─────────┼──────────────┐
                ▼         ▼              ▼
       DeviceResolver  validate=True?  MutableInventory
        .build(host,    SnmpClient      .register(device)
         "v2c", ...)   GET 1.3.6.1.2.1.1.1.0    │
                ▼         │              ▼
              Device      │     _base (frozen) +
                └─────────┘      _overlay dict
                          ▼
              DeviceRecord.model_dump (community → "**********")
```

`cli.main()` runs `verify_tools_are_catalogued(registry)` before `mcp.run()` (`src/nora/cli.py:245`); reads `registry.required_oids_by_tool(("cambium","pmp450i"))` carrying `register_device` post-resign. Prompt §4 Step 4: when `Inventory.get(ip)` raises `DeviceNotFoundError`, orchestrator calls `register_device(host=<ipv4>, community=<operator-provided>)`.

## 3. Module/file changes

| File | Change |
|------|--------|
| `src/nora/drivers/mutable_inventory.py` | NEW. `MutableInventory` + `_InventoryLike` Protocol. Separate file to keep `inventory.py` `frozen=True` purity. |
| `src/nora/drivers/exceptions.py` | mod. Append `DeviceUnreachable`, `InvalidCommunity`, `InvalidHostError`, `DuplicateDeviceError`. |
| `src/nora/drivers/snmp_pmp450i/driver.py` | mod. Ctor `Inventory` → `_InventoryLike` (line 80). Add `register`/`unregister`. |
| `src/nora/drivers/snmp_pmp450i/register_device.py` | NEW. `_register_device_impl(...)` + `DeviceRecord`. |
| `src/nora/server.py` | mod. `@mcp.tool register_device` after line 469. Add `"register_device"` to `__all__`. Drop `snmp_get_pmp450i_radio_metrics` from `_ALLOWED_UNCATALOGUED_TOOLS` (line 626). Fix "five" → "twelve" docstring (line 4). |
| `src/nora/cli.py` | mod. `MutableInventory(seed=Inventory.from_yaml(...))` at line 237-238. |
| `src/nora/prompts/netops_orchestrator.md` | mod. §4 Step 4 fallback after line 48. |
| `data/oid-catalogs/sources/cambium/pmp450i/{15.2.1,15.3.0,25.1.0}.source.json` | mod. Add `"sysDescr": "1.3.6.1.2.1.1.1.0"` to `oids`. |
| `data/oid-catalogs/{cambium,src/nora/data/oid-catalogs}/pmp450i/{15.2.1,15.3.0,25.1.0}.json` | mod. Re-sign BOTH operator + built-in roots. |
| `scripts/sign_catalog.py` | mod. Add `"register_device": ["sysDescr"]` to `TOOLS_V1` (line 80). |
| `.gitignore` | mod. Append `data/devices.yaml`, `data/devices-*.yaml`, `!data/devices.example.yaml`. |
| `data/devices.example.yaml` | mod. Remove false "gitignored" claim line 5. |
| `tests/test_mutable_inventory.py` | NEW. 5 scenarios. |
| `tests/test_register_device.py` | NEW. 7 + wrapper-routing. |
| `tests/test_devices_yaml_gitignore.py` | NEW. 3 scenarios. |
| `tests/test_prompts_register_device_clause.py` | NEW. 3 regex. |
| `tests/test_server.py` | mod. Rename `_eleven_tools` → `_twelve_tools`. |
| `tests/conftest.py` | mod. Add `"sysDescr"` to `_SAMPLE_CATALOG_PAYLOAD`. |
| `tests/test_oid_catalog_integration.py` | mod. Add `"register_device": ["sysDescr"]` to `_INTEGRATION_CATALOG_TOOLS`. |

## 4. API design

### 4.1 `_InventoryLike` + `MutableInventory`

```python
class _InventoryLike(Protocol):
    def get(self, device_id: str) -> Device: ...
    @property
    def device_ids(self) -> list[str]: ...

class MutableInventory:
    def __init__(self, base: Inventory) -> None: ...
    def get(self, device_id: str) -> Device: ...        # overlay → base; raises DeviceNotFoundError
    def register(self, device: Device) -> None: ...     # raises DuplicateDeviceError
    def unregister(self, device_id: str) -> None: ...   # raises DeviceNotFoundError (overlay only)
    @property
    def device_ids(self) -> list[str]: ...              # union sorted
    @property
    def base(self) -> Inventory: ...
```

`Inventory.frozen=True` preserved. `_overlay` is a `dict[str, Device]` guarded by `threading.Lock`; reads lock-free.

### 4.2 Exceptions (appended to `exceptions.py`)

```python
class DeviceUnreachable(DriverError):     # OSError / TimeoutError on sysDescr
class InvalidCommunity(DriverError):      # puresnmp.exc.SnmpError
class InvalidHostError(DriverError):      # IPv4 parse fail
class DuplicateDeviceError(DriverError):  # register collision
```

### 4.3 `register_device` tool

```python
@mcp.tool
def register_device(host: str, community: str, validate: bool = True) -> dict[str, Any]:
    """Ad-hoc-register a PMP 450i radio. validate=True issues a cheap
    sysDescr GET (1.3.6.1.2.1.1.1.0) before insertion. Returns typed
    DeviceRecord with community masked to "**********". Typed error on
    any failure path; inserts NOTHING."""
    return _register_device_impl(
        driver=get_driver(),
        host=host, community=community, validate=validate,
        sanitizer=_sanitizer,
    ).model_dump(mode="json")
```

`DeviceRecord` (frozen Pydantic): `device_id`, `host`, `vendor="cambium"`, `model="pmp450i"`, `firmware="(adhoc)"`, `snmp_version`, `community: SecretStr`, `validated: bool`. `model_dump(mode="json")` masks `community` → `"**********"`.

Error paths: `InvalidHostError(host)` pre-wire; `DeviceUnreachable(host)` on `OSError`/`TimeoutError`; `InvalidCommunity(community)` on `puresnmp.exc.SnmpError`. No insert on any failure. `_ToolLogMiddleware` (`server.py:530-541`) emits `tool=register_device duration_ms=<int> outcome=<success|error>` — no new audit channel.

## 5. Sequence flows

**Happy path.** `register_device("10.53.20.5","MEXI2-BB-RW",True)` → `DeviceResolver.build` → `Device` → `SnmpClient.get_oid("1.3.6.1.2.1.1.1.0")` → sysDescr bytes → `driver.register(device)` → `MutableInventory._overlay[id]=device` → `DeviceRecord.model_dump`. Later `snmp_get_ap_summary(device_id)` → `driver._inventory.get` → overlay hit.

**Failure path.** sysDescr raises `OSError` → `DeviceUnreachable("10.53.20.5")`; no `driver.register` call; `device_ids` unchanged.

## 6. Inventory.get() call site migration

All 8 call sites route through `MutableInventory.get` because `cli.py:237-238` passes the wrapper to `Pmp450iDriver`. Constructor signature change back-compat: `Inventory` already satisfies the Protocol.

| File:Line | Method |
|-----------|--------|
| `snmp_pmp450i/driver.py:102` | `fetch_radio_metrics` |
| `snmp_pmp450i/driver.py:227` | `report_firmware` |
| `snmp_pmp450i/summaries.py:145` | `fetch_ap_summary`/`fetch_frame_utilization` |
| `snmp_pmp450i/subscribers.py:496` | `fetch_sm_table` |
| `snmp_pmp450i/subscribers.py:582` | `fetch_sm_detailed_diagnostics` |
| `snmp_pmp450i/spectrum.py:181` | `fetch_spectrum` |
| `snmp_pmp450i/migrate.py:248` | `fetch_migrate` |

New pinning test: `test_register_device_routes_through_wrapper` injects `MutableInventory`, registers, asserts `snmp_get_ap_summary(device_id)` succeeds — overlay reaches the driver.

## 7. Test architecture (strict TDD)

- `R-NEW-1-S1`: `test_server.py::test_server_exposes_exactly_twelve_tools`
- `R-NEW-1-S2`: `test_register_device.py::test_register_device_tools_list_schema`
- `R-NEW-2-S2`: `test_register_device.py::test_register_device_payload_masks_credentials`
- `R-NEW-6-S3`: `test_oid_catalog_integration.py::test_every_resigned_baseline_tools_envelope_includes_register_device` (param)
- `R-NEW-6-S4`: `test_oid_catalog_integration.py::test_register_device_passes_registration_guard_via_catalog_envelope`
- `R-NEW-6-S5`: `test_server.py::test_snmp_get_pmp450i_radio_metrics_no_longer_in_allowlist`
- `Inv-1-S1..S5`: `test_mutable_inventory.py::{register_inserts_new_device, register_rejects_duplicate_device_id, unregister_removes_runtime_device, unregister_unknown_id_raises_device_not_found, get_delegates_to_base_inventory_for_yaml_loaded_entry}`
- `Tool-S1..S7`: `test_register_device.py::{success_with_validate, rejects_unreachable_host, validate_false_inserts_without_wire, rejects_malformed_host, rejects_invalid_community, two_calls_return_distinct_device_ids, routes_through_wrapper}`
- `Prompt-S1..S3`: `test_prompts_register_device_clause.py::{mentions_register_device, references_sysdescr_validation, does_not_instruct_orchestrator_to_echo_community}`
- `Gitignore-S1..S3`: `test_devices_yaml_gitignore.py::{yaml_is_gitignored, example_remains_tracked, stale_claim_removed}`
- `Cat-S1..S2`: `test_oid_catalog_integration.py::{every_resigned_baseline_hmac_verifies, resigned_baseline_oids_includes_sysdescr}` (param)

## 8. Catalog re-sign procedure

1. Operator (or developer) edits each `data/oid-catalogs/sources/cambium/pmp450i/{15.2.1,15.3.0,25.1.0}.source.json` — add `"sysDescr": "1.3.6.1.2.1.1.1.0"` to the `oids` map.
2. Operator edits `scripts/sign_catalog.py:80` (`TOOLS_V1` dict) — add `"register_device": ["sysDescr"]`.
3. Sign the operator root:
   ```bash
   NORA_OID_CATALOG_SIGNING_KEY="<operator-key>" python scripts/sign_catalog.py --all
   ```
4. Sign the built-in root in the same PR (mandatory; without this, boot aborts on `CatalogVerificationError`):
   ```bash
   NORA_OID_CATALOG_SIGNING_KEY="$BUILTIN_BASELINE_SIGNING_KEY" python scripts/sign_catalog.py --all --output-root src/nora/data/oid-catalogs
   ```
5. CI gate: `pytest tests/test_oid_catalog_integration.py` — parametrized HMAC + envelope tests assert every baseline carries the new envelope and verifies cleanly. Failure aborts CI.
6. Local sanity check: `OidCatalogRegistry.verify_all(settings)` returns no `CatalogVerificationError`.

## 9. Migration / rollout

Single PR to `main`. Commit order: (1) `feat(drivers): MutableInventory + exceptions` (failing tests first); (2) `refactor(driver): route Inventory.get() through MutableInventory`; (3) `feat(mcp): register_device + DeviceRecord`; (4) `feat(catalog): re-sign PMP 450i baselines + built-in`; (5) `refactor(server): retire snmp_get_pmp450i_radio_metrics from _ALLOWED_UNCATALOGUED_TOOLS`; (6) `chore: gitignore data/devices.yaml + fix stale devices.example.yaml`; (7) `docs(prompt): §4 Step 4 register_device fallback` + regex tests; (8) `test(server): rename _eleven_tools to _twelve_tools`. Final: full suite + ruff + mypy --strict. PR body `Closes #42`.

## 10. Risks

| # | Risk | Mitigation |
|---|------|------------|
| 1 | Wrapper drift — missed `Inventory.get()` bypasses overlay (M×H) | 8 sites in §6; `test_register_device_routes_through_wrapper` proves overlay reaches driver via `snmp_get_ap_summary`. |
| 2 | Botched re-sign invalidates 3 baselines (L×C) | `verify_all()` after resign; CI parametrized HMAC test aborts; built-in re-signed same PR. |
| 3 | `MutableInventory` not thread-safe (L×M) | `Device` Pydantic-frozen; only `_overlay` mutable. `threading.Lock` serializes `register`/`unregister`; reads lock-free. |
| 4 | Prompt clause heuristic bypassed (M×M) | Failure mode = "operator sees `DeviceNotFoundError` again" — same as today. |
| 5 | v3 creds out of scope (L×L) | Spec explicit; v3 follow-up. |

## 11. Open questions

None — all decisions resolved upstream in preflight and proposal.

## Threat Matrix

`N/A` — no routing, shell, subprocess, VCS/PR automation, executable-file classification, or process-integration boundary. `register_device` reuses `SnmpClient.get_oid`; `_ToolLogMiddleware` is in-process; re-sign is operator-side gated by existing HMAC verifier.