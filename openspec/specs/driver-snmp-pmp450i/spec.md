# driver-snmp-pmp450i Specification

## Purpose

Read-only SNMP driver for Cambium PMP 450i. Exposes one FastMCP tool that fetches a typed `RadioMetricsReport` over SNMPv2c or SNMPv3 (auth+priv), refuses every write op, and obeys typed-return / sanitizer / no-secrets contracts. SCOPE.md §4-A.

## Requirements

### Requirement: Protocol Support — v2c AND v3

MUST support SNMPv2c and SNMPv3 (USM auth+priv), data-driven per `Device`. No runtime internet or MIB fetch.

#### Scenario: v2c fetch returns typed report

- GIVEN a `Device` with `snmp_version == "v2c"` and a community string
- WHEN the MCP tool runs
- THEN a typed `RadioMetricsReport` is returned
- AND no `dict` or `Any` field appears in the response

#### Scenario: v3 auth+priv fetch returns a typed report

- GIVEN a `Device` with `snmp_version == "v3"`, `auth_password` AND `priv_password`
- WHEN the tool runs
- THEN the typed report is returned over the encrypted session

#### Scenario: malformed OID raises a typed error

- GIVEN the agent returns non-numeric or empty values
- WHEN the tool runs
- THEN a typed error is raised carrying a sanitized message

### Requirement: Read-Only Enforcement

The driver MUST refuse every write op. Any call to `set`, `update`, `setbulk`, `bulk_set`, or `write` under `src/nora/drivers/` MUST raise `RefusesWriteError`. A property test MUST prove the catalogue is empty.

#### Scenario: write attempt rejected

- GIVEN a tool call resolving to a write verb
- WHEN it reaches the driver
- THEN `RefusesWriteError` is raised and no SNMP wire write is sent

#### Scenario: AST lint forbids write identifiers

- GIVEN a file under `src/nora/drivers/` containing a write identifier
- WHEN `ruff check .` runs
- THEN the violation is reported and exit code is non-zero

### Requirement: Strictly Typed Return

Successful responses MUST be a Pydantic `RadioMetricsReport`. Free-text errors MUST pass through `Sanitizer`; the typed model MUST NOT (opt-out).

#### Scenario: typed opts out; free-text sanitizes

- GIVEN a `RadioMetricsReport` carrying integer dBm values AND a raised error with a private IPv4 literal
- WHEN the tool serialises / returns
- THEN the integers are byte-identical before and after sanitization
- AND the IPv4 literal is replaced with an alias

### Requirement: Auto-Trace Integration

The tool MUST be `@mcp.tool`-registered so the `session-journal` middleware records every invocation.

#### Scenario: a SessionStep is recorded

- GIVEN an active session with `trace == []`
- WHEN the tool returns
- THEN `trace.length == 1`, `tool` equals the registered name, and `input["device_id"]` is preserved

### Requirement: Device Focus Binding

The driver MUST call `nora_session_set_focus(device_id)` before the fetch.

#### Scenario: focus is set before fetch

- GIVEN `focus_device_id is None`
- WHEN the tool runs with `device_id == "ap-7400-01"`
- THEN `focus_device_id == "ap-7400-01"` and `devices_reviewed == ["ap-7400-01"]`

### Requirement: Failure Surfaces Typed Errors

Device-not-found, network unreachable, and SNMP timeout MUST each surface a typed exception. No bare `Exception` may leak.

#### Scenario: each error is typed

- GIVEN an unknown `device_id`, a closed agent port, or an unresponsive agent
- WHEN the tool runs in each case
- THEN `DeviceNotFoundError`, `NetworkUnreachableError`, or `SnmpTimeoutError` is raised respectively

### Requirement: Air-Gap & No Secrets

No `.py` under `src/nora/drivers/` MAY import `requests`, `httpx`, `urllib.request`, `socket`, `ssl`, `http.client`, or any DNS resolver (static scan + runtime test). `community`, `auth_password`, `priv_password` MUST NOT appear in any field, error, or log line.

#### Scenario: network-free and secrets-free

- GIVEN every `.py` under `src/nora/drivers/` AND a `Device` with `community == "private"`
- WHEN the scan / test run AND the tool returns
- THEN zero banned-import matches, no mocked symbol called, and the literal `"private"` absent

### Requirement: OID Resolution Caching (SHOULD)

The driver SHOULD cache OIDs within one tool call. No cross-call cache.

#### Scenario: repeated OID lookups reuse cache

- GIVEN a call needing the same OID twice
- WHEN the driver runs
- THEN only one SNMP GET frame is emitted for that OID

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
## Dependencies

`nora-mcp-server`, `secure-configuration`, `telemetry-sanitizer`, `session-journal`, `oid-catalog`, `prompt-registry`.
