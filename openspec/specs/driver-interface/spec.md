# driver-interface Specification

## Purpose

Defines the `DeviceDriverInterface` Protocol — the seam that lets NORA core talk to any per-vendor driver without leaking vendor detail — plus `DeviceResolver`, the IP-direct path that resolves ad-hoc equipment without mutating `devices.yaml` (issue #14). Slice 1 of the PMP 450i production surface. Closes #14 once slice 1 lands; ADR #17 Test 9 depends on `report_firmware()`.

## Requirements

### Requirement: Protocol Contract — Six Methods

`DeviceDriverInterface` SHALL declare six methods: `fetch_radio_metrics`, `fetch_ap_summary`, `fetch_frame_utilization`, `fetch_sm_table`, `fetch_sm_detailed_diagnostics`, and `report_firmware`. The Protocol SHALL be runtime-checkable. `Pmp450iSnmpDriver` SHALL adapt the existing `Pmp450iDriver` so its public surface is unchanged.

#### Scenario: `Protocol` runtime check passes for the concrete adapter

- GIVEN `Pmp450iSnmpDriver(inventory, catalog_registry)` constructed
- WHEN `isinstance(driver, DeviceDriverInterface)` runs
- THEN the check returns `True` AND every declared method is callable

#### Scenario: a missing method fails the runtime check

- GIVEN a stub class lacking `fetch_frame_utilization`
- WHEN `isinstance(stub, DeviceDriverInterface)` runs
- THEN the check returns `False` AND the boot sequence aborts before serving MCP

### Requirement: `report_firmware()` Typed Return

`report_firmware(device_id: str) -> Version` SHALL return a `packaging.version.Version` parsed from the agent's `sysDescr` (or the equivalent vendor OID). The method MUST run before every catalog-resolve so the registry's semver logic (ADR #17) sees real fleet firmware, not a hard-coded pin.

#### Scenario: driver reports the firmware the agent advertises

- GIVEN a `snmpsim` agent with `sysDescr` containing `15.3.0`
- WHEN `report_firmware("ap-7400-01")` runs
- THEN `Version("15.3.0")` is returned AND no network error is raised

### Requirement: `DeviceResolver.build` — IP-Direct, Ephemeral, No YAML Mutation

`DeviceResolver.build(host, snmp_version, creds) -> Device` SHALL return a frozen `Device` whose `device_id` is the collision-safe stem `f"adhoc-{host}-{secrets.token_hex(3)}"`. The resolver MUST NOT open, write to, or mutate `Settings.nora_devices_inventory_path`. All credential fields MUST be `pydantic.SecretStr`.

#### Scenario: IP-direct resolution builds an ephemeral device

- GIVEN `host="192.0.2.10"`, `snmp_version="v3"`, valid `auth_password` + `priv_password`
- WHEN `DeviceResolver.build(...)` runs
- THEN the returned `Device` has `device_id == "adhoc-192.0.2.10-<6hex>"` AND `devices.yaml` bytes are unchanged

#### Scenario: stem collisions are resolved by entropy suffix

- GIVEN two consecutive `DeviceResolver.build(host="192.0.2.10", ...)` calls
- WHEN both run
- THEN the two `device_id` stems differ AND both share the host prefix

### Requirement: SecretStr Safety — No Plaintext In Repr

Every credential field on a `Device` returned by either resolution path MUST round-trip through `SecretStr.get_secret_value()` only inside the SNMP client factory. `repr(device)`, `str(device)`, and `model_dump()` MUST NOT contain the literal community / password bytes.

#### Scenario: `repr` masks credentials

- GIVEN a `Device` with `community.get_secret_value() == "private"`
- WHEN `repr(device)` and `device.model_dump()` run
- THEN neither contains the literal `"private"` AND `**********` appears in their place

### Requirement: Inventory Path — Full Back-Compatibility

The inventory path (`Inventory.from_yaml` → `Pmp450iDriver.fetch_radio_metrics`) MUST remain green. `test_driver_snmp_pmp450i`, `test_driver_snmpsim_{v2c,v3}`, `test_driver_snmp450i_readonly`, and `test_driver_airgap` SHALL pass unchanged after slice 1.

#### Scenario: inventory fetch returns a typed report

- GIVEN a `Device` registered in `devices.yaml`
- WHEN `driver.fetch_radio_metrics(device_id)` runs
- THEN a typed `RadioMetricsReport` is returned AND no exception is raised

### Requirement: Sanitizer Boundary

Every tool response field that would echo `target_ip` / `host` / `port` MUST pass through `Sanitizer.sanitize(...)` before serialisation. The Protocol returns typed scalars; the MCP wrapper sanitises.

#### Scenario: target host redacted on the wire

- GIVEN a tool call against the ad-hoc device whose `host == "192.0.2.10"`
- WHEN the response is serialised
- THEN `192.0.2.10` is replaced by a synthetic alias AND the structured `device_id` is byte-identical

### Requirement: Banned-Imports (Air-Gap)

No module under `src/nora/drivers/` MAY import `requests`, `httpx`, `urllib.request`, `socket`, `ssl`, `http.client`, or any DNS resolver. `DeviceResolver.build` MUST NOT perform DNS — it accepts an IP literal or hostname but skips resolution when the literal already parses as IPv4.

#### Scenario: static AST scan finds zero banned imports

- GIVEN every `.py` under `src/nora/drivers/`
- WHEN the scan runs
- THEN zero matches are found

## Cross-References

`driver-snmp-pmp450i`, `oid-catalog`, `pmp450i-radio-tools`.

## Covered Named Tests (Slice 1)

`ip_direct_resolution_builds_ephemeral_device`, `secret_str_safety_no_plaintext_in_repr`, `report_firmware_returns_typed_version`, `inventory_path_still_works_no_regression`.