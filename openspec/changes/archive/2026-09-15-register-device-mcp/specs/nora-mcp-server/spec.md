# Delta for nora-mcp-server

> Modified by `2026-09-15-register-device-mcp` — closes issue #42 (IPv4-literal resolution gap). Adds the `register_device` MCP tool to the global twelve-tool surface; updates the sanitizer-bound-at-tool-boundary requirement to cover the new tool plus its `SecretStr` community masking; renames the catalog registration-guard heading to reflect the post-resign catalog envelope (`register_device` + `snmp_get_pmp450i_radio_metrics` are now in the `tools` map of every re-signed PMP 450i baseline; the legacy `_ALLOWED_UNCATALOGUED_TOOLS` entry for `snmp_get_pmp450i_radio_metrics` is retired).


## RENAMED Requirements

### Requirement: R-NEW-6 — Tool-Registration Guard (Uncatalogued Tools Rejected) -> R-NEW-6 — Tool-Registration Guard (Catalog Re-Signed)

(Reason: heading previously described only the rejection half of the guard; after the PMP 450i catalog re-sign the requirement now also covers HMAC verification of every catalogued `(vendor, model, firmware)` triple, the `tools` envelope including `register_device` + `sysDescr`, and the retirement of the `snmp_get_pmp450i_radio_metrics` allow-list entry)

(Migration: `openspec/specs/nora-mcp-server/spec.md` heading updated; no test or doc references outside this file use the heading text — the canonical reference is the `R-NEW-6` identifier, which is unchanged)
## MODIFIED Requirements

### Requirement: R-NEW-1 — Four `@mcp.tool` Registrations

The server SHALL register exactly twelve `@mcp.tool`-decorated functions on the global `mcp = FastMCP("nora")`: one driver + three intervention memory + `save_intervention_record` + six radio-link tools (`snmp_get_ap_summary`, `snmp_get_frame_utilization`, `snmp_get_sm_table`, `snmp_get_sm_detailed_diagnostics`, `snmp_run_spectrum_analysis`, `snmp_migrate_radio_frequency`) + `register_device(host: str, community: str, validate: bool = True) -> DeviceRecord`. All twelve names SHALL be re-exported in `__all__`. (Previously: exactly eleven registrations.)

#### Scenario: server module exports the twelve tool names

- GIVEN `src/nora/server.py` imports the driver + three intervention callables + the writer + six radio-link callables + the `register_device` wrapper
- WHEN the twelve names are imported from `nora.server`
- THEN the imports succeed AND all twelve names appear in `nora.server.__all__`

#### Scenario: `tools/list` over stdio returns twelve tools in the registered order

- GIVEN the server booted via `mcp.run()` over stdio
- WHEN a client calls `tools/list`
- THEN the response contains the twelve tool names AND their `inputSchema` matches each registered signature AND `register_device`'s schema declares `host: str`, `community: str`, `validate: bool` (default `true`)

### Requirement: R-NEW-2 — Sanitizer Bound at Tool Boundary

Every free-text field in the twelve tool responses SHALL pass through `Sanitizer.sanitize(...)` before serialisation. Structured top-level fields SHALL bypass. The `register_device` payload's `community` field SHALL be masked at the Pydantic boundary via `SecretStr` (rendered as `"**********"` in `model_dump(mode="json")`). (Previously: eleven tools; now twelve.)

#### Scenario: free-text fields in the radio-link tools are sanitized

- GIVEN a tool response whose free-text field contains the literal `192.0.2.10`
- WHEN the response is serialised
- THEN the literal is replaced by a synthetic alias AND typed scalars (`carrier_frequency_mhz`, `result["rolled_back"]`) are byte-identical

#### Scenario: free-text fields in `register_device` error messages are sanitized

- GIVEN a `DeviceUnreachable` error whose message contains the literal `192.0.2.10`
- WHEN the tool serialises the response
- THEN the literal is replaced by a synthetic alias AND typed `DeviceRecord` fields (`device_id`, `host`) are byte-identical

#### Scenario: credential masking on `register_device` success payload

- GIVEN a successful `register_device("192.0.2.10", "MEXI2-BB-RW")` call
- WHEN `device.model_dump(mode="json")` runs
- THEN the `community` field renders as `"**********"` AND the literal `MEXI2-BB-RW` does NOT appear

### Requirement: R-NEW-6 — Tool-Registration Guard (Catalog Re-Signed)

`src/nora/cli.py` SHALL run a registration guard that walks every function registered against the global `mcp` instance. The guard MUST raise a typed `UncataloguedToolError` if the tool name is absent from `OidCatalogRegistry.REQUIRED_OIDS` for every catalogued `(vendor, model, firmware)` triple. The guard runs ONCE at boot; the rejection MUST abort before `mcp.run(show_banner=False)`. After the PMP 450i catalog re-sign, every re-signed baseline (`15.2.1`, `15.3.0`, `25.1.0`) carries `"register_device": ["sysDescr"]` in its `tools` envelope AND `sysDescr: "1.3.6.1.2.1.1.1.0"` in its `oids` map; each `hmac_sha256` SHALL pass `OidCatalogRegistry.verify_all`. The `_ALLOWED_UNCATALOGUED_TOOLS` allow-list at `src/nora/server.py:626` SHALL no longer contain `snmp_get_pmp450i_radio_metrics` (its envelope is added to all three baselines by the same re-sign); the remaining four entries are unchanged. (Previously: `register_device` uncatalogued; `snmp_get_pmp450i_radio_metrics` was a fifth allow-list entry.)

#### Scenario: an uncatalogued `@mcp.tool` is rejected at boot

- GIVEN a developer adds `@mcp.tool def snmp_get_rogue_metric(device_id: str): ...` with no catalog entry
- WHEN `cli.main()` runs the registration guard
- THEN `UncataloguedToolError` is raised naming `(tool_name, reason="no OID catalog entry")` AND `mcp.run()` is never invoked

#### Scenario: every re-signed PMP 450i baseline HMAC verifies

- GIVEN the three catalog files at `data/oid-catalogs/cambium/pmp450i/{15.2.1,15.3.0,25.1.0}.json` after `scripts/sign_catalog.py` re-sign (parametrized across the three versions)
- WHEN `OidCatalogRegistry.verify_all()` runs at boot
- THEN all three HMACs verify AND no `CatalogSignatureError` is raised AND `mcp.run(show_banner=False)` proceeds

#### Scenario: every re-signed baseline's tools envelope includes register_device and sysDescr OID

- GIVEN the three re-signed catalog files (parametrized across the three versions)
- WHEN `OidCatalogRegistry` loads `(cambium, pmp450i, <version>)`
- THEN the `tools` map contains `"register_device": ["sysDescr"]` AND the `oids` map contains `"sysDescr": "1.3.6.1.2.1.1.1.0"`

#### Scenario: `register_device` passes the registration guard via its catalog envelope

- GIVEN `register_device` is `@mcp.tool`-decorated AND present in all three re-signed catalogs
- WHEN `cli.main()` runs the registration guard
- THEN `register_device` is accepted without an allow-list entry AND `mcp.run(show_banner=False)` proceeds

#### Scenario: `snmp_get_pmp450i_radio_metrics` no longer requires the allow-list

- GIVEN the three re-signed catalogs now carry `snmp_get_pmp450i_radio_metrics` in the `tools` envelope
- WHEN `_ALLOWED_UNCATALOGUED_TOOLS` is inspected at `src/nora/server.py:626`
- THEN `"snmp_get_pmp450i_radio_metrics"` is NOT in the frozenset AND the four remaining entries are unchanged
