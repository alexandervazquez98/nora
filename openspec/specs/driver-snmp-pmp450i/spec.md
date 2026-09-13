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

## Dependencies

`nora-mcp-server`, `secure-configuration`, `telemetry-sanitizer`, `session-journal`, `oid-catalog`, `prompt-registry`.
