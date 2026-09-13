# oid-catalog-integration Specification

## Purpose

Materialises ADR #17 Tests 9 + 10 (issue #24): exercises the read-path from `DeviceDriverInterface.report_firmware()` through `OidCatalogRegistry.resolve(...)` to the wire frame, and proves that every `@mcp.tool` declared in `OidCatalogRegistry.REQUIRED_OIDS` is signed for some `(vendor, model, firmware)`. Tools without a catalog entry SHALL be rejected at `@mcp.tool()` registration time — not silently exposed to MCP clients.

## Requirements

### Requirement: Dynamic Firmware Resolution Before Every Query

For every `@mcp.tool` that touches an SNMP device, `OidCatalogRegistry.resolve((vendor, model, firmware))` MUST run with the value reported by `DeviceDriverInterface.report_firmware(...)`. The minor-mismatch fallback (closest lower minor) and the literal warning `"OID catalog fallback: requested X, using Y (minor mismatch)"` apply through the tool path. A strict-major mismatch SHALL hard-fail the query with a typed `CatalogNotFoundError`.

#### Scenario: minor-mismatch warning emitted via the read path

- GIVEN `report_firmware()` returns `Version("15.3.0")` AND the registry holds only `15.2.1`
- WHEN `snmp_get_ap_summary(device_id)` runs end-to-end
- THEN the catalog at `15.2.1` is used AND stderr carries the literal `"OID catalog fallback: requested 15.3.0, using 15.2.1 (minor mismatch)"`

#### Scenario: major-mismatch blocks the driver query with a typed error

- GIVEN `report_firmware()` returns `Version("16.0.0")` AND the registry holds only `15.x`
- WHEN any read tool runs
- THEN `CatalogNotFoundError` is raised AND its message names both majors

### Requirement: Per-Tool `REQUIRED_OIDS` Index

`OidCatalogRegistry` SHALL carry a per-tool index mapping `@mcp.tool` name → set of OID names it consumes. The index is built from the catalog JSON envelope (`"tools": {"snmp_get_ap_summary": ["radioDownlinkRate", ...]}`). Boot SHALL fail if any catalogued tool name lacks an index entry for its `(vendor, model)` triple.

#### Scenario: every catalogued tool has an index entry

- GIVEN the catalog envelope for `(cambium, pmp450i, 15.2.1)` lists six tool names
- WHEN boot validates the per-tool index
- THEN each name resolves to a non-empty set of OID names AND zero names are missing

### Requirement: Tool-Registration Guard — Reject Uncatalogued Tools

`mcp.tool()` registration SHALL refuse any function whose name is NOT present in `OidCatalogRegistry.REQUIRED_OIDS` for every catalogued `(vendor, model, firmware)`. The guard MUST raise a typed `UncataloguedToolError` at decoration time (boot fails fast), not silently expose the tool to MCP clients. Tools explicitly listed in an `ALLOWED_UNCATALOGUED` allow-list (none today) MAY bypass.

#### Scenario: a new tool without OID registration is rejected at decoration time

- GIVEN a developer adds `@mcp.tool def snmp_get_rogue_metric(device_id: str): ...` with no catalog entry
- WHEN boot runs the registration guard
- THEN `UncataloguedToolError` is raised naming `(tool_name="snmp_get_rogue_metric", reason="no OID catalog entry")` AND the boot aborts before serving MCP

#### Scenario: a tool WITH a catalog entry passes the guard

- GIVEN `snmp_get_ap_summary` is registered with an `@mcp.tool` decorator
- WHEN boot runs the registration guard
- THEN the tool is registered AND `tools/list` over stdio returns its name

### Requirement: E2E Coverage Of The Read Path

`tests/test_oid_catalog_integration.py` SHALL exercise the full chain: `@mcp.tool` call → catalog resolver → driver wire frame, asserting that the catalog chosen by the resolver is the one whose OIDs hit the wire.

#### Scenario: the wire OIDs match the chosen catalog

- GIVEN `report_firmware()` returns `15.3.0` and the registry resolves to `15.2.1`
- WHEN `snmp_get_ap_summary(device_id)` runs against `snmpsim`
- THEN the OIDs in the recorded GET frames equal `15.2.1`'s catalog entries AND the literal warning is logged

#### Scenario: pre-release firmware matches the bare version without warning

- GIVEN `report_firmware()` returns `Version("15.2.1-rc.1")` AND the registry holds `15.2.1`
- WHEN the tool runs
- THEN the catalog at `15.2.1` is used AND no fallback warning is emitted

## Cross-References

- `driver-interface` — `report_firmware()` is the upstream of the resolver call.
- `oid-catalog` — semver-aware resolution, HMAC boot verification, per-`(vendor, model)` required-OID set.
- `pmp450i-radio-tools` — the six new `@mcp.tool`s are the subject of the registration guard.
- `nora-mcp-server` — `mcp.tool()` registration is the surface the guard wraps.

## Covered Named Tests (Slice 5)

`dynamic_resolution_applies_catalog_versioning_before_query`, `minor_mismatch_warning_during_read_path_e2e`, `major_mismatch_blocks_driver_query_typed`, `unified_tool_catalog_references_required_oids_per_tool`, `new_tool_without_oid_registration_rejected_at_registration_time`.