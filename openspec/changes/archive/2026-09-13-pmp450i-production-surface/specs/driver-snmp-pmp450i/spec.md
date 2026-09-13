# Delta for driver-snmp-pmp450i

> Modified by `2026-09-13-pmp450i-production-surface` — adds the `DeviceDriverInterface` seam, the `report_firmware()` typed return, and the dual inventory / IP-direct resolution paths. `Pmp450iDriver` public surface preserved.

## ADDED Requirements

### Requirement: DeviceDriverInterface Is The Driver Seam

`Pmp450iSnmpDriver` SHALL be the public class behind `DeviceDriverInterface` and SHALL adapt `Pmp450iDriver` (read-only facade) without altering its public methods. The existing `Pmp450iDriver.fetch_radio_metrics(...)` continues to be reachable as a delegate call. (Previously: there was no `Protocol`; `Pmp450iDriver` was the only driver surface.)

#### Scenario: Pmp450iDriver remains reachable through the new seam

- GIVEN `Pmp450iSnmpDriver(inventory, catalog_registry)` constructed
- WHEN `driver.fetch_radio_metrics(device_id)` runs
- THEN the typed `RadioMetricsReport` returned is byte-identical to the pre-slice-1 result AND no test in the existing suite changes

### Requirement: `report_firmware()` Part Of The Public Contract

`Pmp450iSnmpDriver` SHALL expose `report_firmware(device_id: str) -> Version`, returning a `packaging.version.Version` parsed from the agent's `sysDescr` (or the equivalent vendor OID). The return is the upstream of `OidCatalogRegistry.resolve(...)`. (Previously: firmware was a hard-coded field on `Device`; no live query existed.)

#### Scenario: report_firmware returns the agent's advertised version

- GIVEN a `snmpsim` agent advertising `15.3.0`
- WHEN `driver.report_firmware(device_id)` runs
- THEN `Version("15.3.0")` is returned AND no exception leaks

### Requirement: Inventory Path Preserved — No Regression

`Inventory.from_yaml(...)` → `Pmp450iDriver.fetch_radio_metrics(...)` SHALL continue to operate exactly as today. The new IP-direct resolution path (`DeviceResolver.build`) is additive and MUST NOT mutate `devices.yaml`. (Previously: inventory was the only resolution path; ad-hoc IPs raised `DeviceNotFoundError`.)

#### Scenario: existing inventory fetch stays green

- GIVEN a populated `devices.yaml`
- WHEN the existing tests `test_driver_snmp_pmp450i`, `test_driver_snmpsim_{v2c,v3}`, `test_driver_snmp450i_readonly`, `test_driver_airgap` run
- THEN all four pass unchanged AND coverage of `src/nora/drivers/` stays ≥ 85%

### Requirement: Ad-Hoc Path Exists Alongside Inventory

`DeviceResolver.build(host, snmp_version, creds)` SHALL return a frozen `Device` whose `device_id` is `f"adhoc-{host}-{secrets.token_hex(3)}"`, allowing the driver to talk to equipment not declared in `devices.yaml`. (Previously: `Inventory.get(unknown_id)` raised `DeviceNotFoundError`; #14 blocker.)

#### Scenario: ad-hoc resolution succeeds for an inventory-absent host

- GIVEN `host="192.0.2.10"`, valid v3 credentials, no `devices.yaml` entry
- WHEN the tool resolves the device via `DeviceResolver.build`
- THEN a frozen `Device` is returned AND the wire frame is sent against `192.0.2.10`

### Requirement: Sanitizer Boundary On Tool Responses

The driver returns typed models; the MCP wrapper sanitises free-text error messages and target-host literals before serialisation. The driver MUST NOT pre-sanitise typed return values (matches the existing `nora-mcp-server` R-NEW-2 contract).

#### Scenario: target host is masked in tool response, typed scalars are untouched

- GIVEN an `NetworkUnreachableError` carrying `target == "192.0.2.10:161"`
- WHEN the tool returns the serialised error
- THEN the literal `192.0.2.10` is replaced by a synthetic alias AND any typed scalar (e.g., `report.firmware`) is byte-identical

### Requirement: Air-Gap Extension For New Resolution Path

`DeviceResolver` SHALL NOT perform DNS, TCP connect probes, or any network reachability check during `build(...)`. The resolver only constructs the frozen `Device`; reachability is a driver-layer concern. The existing air-gap AST guard (`test_driver_airgap`) is extended to cover the new module.

#### Scenario: AST scan rejects DNS / socket imports in the resolver

- GIVEN a contributor adds `import socket` to `drivers/resolver.py`
- WHEN `test_driver_airgap` runs
- THEN the test fails naming `(drivers/resolver.py, <line>, "socket")`