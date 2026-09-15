# Delta: register_device MCP tool + orchestrator prompt update

**Change**: `2026-09-15-register-device-mcp`
**Issue**: closes #42
**Capability MODIFIED**: `nora-mcp-server`
**Capability ADDED**: `ad-hoc-device-registration`

> Closes the IPv4-literal resolution gap (issue #42): adds the `register_device` MCP tool, a `MutableInventory` wrapper that preserves `Inventory.frozen=True`, a §4 Step 4 prompt fallback, a `data/devices.yaml` gitignore fix, and the PMP 450i catalog re-sign that retires the legacy `_ALLOWED_UNCATALOGUED_TOOLS` entry for `snmp_get_pmp450i_radio_metrics`. Persistence stays in-memory; YAML writes are out of scope.

## MODIFIED Requirements (under `nora-mcp-server`)

### Requirement: R-NEW-1 — Four `@mcp.tool` Registrations (Updated Count)

The server SHALL register exactly twelve `@mcp.tool`-decorated functions on the global `mcp = FastMCP("nora")`: one driver + three intervention memory + `save_intervention_record` + six radio-link tools (`snmp_get_ap_summary`, `snmp_get_frame_utilization`, `snmp_get_sm_table`, `snmp_get_sm_detailed_diagnostics`, `snmp_run_spectrum_analysis`, `snmp_migrate_radio_frequency`) + `register_device(host: str, community: str, validate: bool = True) -> DeviceRecord`. All twelve names SHALL be re-exported in `__all__`. (Previously: exactly eleven registrations.)

#### Scenario: server module exports the twelve tool names

- GIVEN `src/nora/server.py` imports the driver + three intervention callables + the writer + six radio-link callables + the `register_device` wrapper
- WHEN the twelve names are imported from `nora.server`
- THEN the imports succeed AND all twelve names appear in `nora.server.__all__`

#### Scenario: `tools/list` over stdio returns twelve tools in the registered order

- GIVEN the server booted via `mcp.run()` over stdio
- WHEN a client calls `tools/list`
- THEN the response contains the twelve tool names AND their `inputSchema` matches each registered signature AND `register_device`'s schema declares `host: str`, `community: str`, `validate: bool` (default `true`)

### Requirement: R-NEW-2 — Sanitizer Bound at Tool Boundary (Updated Count)

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

## ADDED Requirements (capability `ad-hoc-device-registration`)

### Requirement: Inventory Mutation Contract — `MutableInventory` Wrapper

A `MutableInventory` wrapper SHALL hold a `dict[str, Device]` plus a read-through reference to a frozen `Inventory`. It SHALL expose `register(device: Device) -> None`, `unregister(device_id: str) -> None`, `get(device_id: str) -> Device`, and `device_ids -> list[str]`. The wrapped `Inventory.frozen=True` invariant is preserved. A new typed `DuplicateDeviceError` SHALL be raised on `register` of an existing `device_id`.

#### Scenario: register inserts a new device

- GIVEN a `MutableInventory(seed=Inventory.from_yaml(...))` whose seed is empty
- WHEN `wrapper.register(Device(device_id="adhoc-192-0-2-10-abcdef", ...))` runs
- THEN `wrapper.get("adhoc-192-0-2-10-abcdef")` returns the device AND `wrapper.device_ids` contains the new id

#### Scenario: register rejects duplicate device_id with DuplicateDeviceError

- GIVEN a `MutableInventory` containing `device_id="ap-7400-01"`
- WHEN `wrapper.register(Device(device_id="ap-7400-01", ...))` runs
- THEN `DuplicateDeviceError("ap-7400-01")` is raised AND no replacement happens AND `wrapper.get("ap-7400-01")` returns the original device

#### Scenario: unregister removes by device_id

- GIVEN a `MutableInventory` containing `device_id="ap-7400-01"`
- WHEN `wrapper.unregister("ap-7400-01")` runs
- THEN `wrapper.get("ap-7400-01")` raises `DeviceNotFoundError` AND `wrapper.device_ids` does NOT contain `"ap-7400-01"`

#### Scenario: unregister of unknown id raises DeviceNotFoundError

- GIVEN a `MutableInventory` empty
- WHEN `wrapper.unregister("nonexistent")` runs
- THEN `DeviceNotFoundError("nonexistent")` is raised

#### Scenario: read-through lookup delegates to underlying Inventory

- GIVEN a `MutableInventory(seed=Inventory.from_yaml(path_with_ap_7400_01.yaml))`
- WHEN `wrapper.get("ap-7400-01")` runs
- THEN the returned device equals the YAML-loaded entry AND no mutation occurred on the underlying `Inventory`

### Requirement: `register_device` MCP Tool

A new `@mcp.tool register_device(host: str, community: str, validate: bool = True) -> DeviceRecord` SHALL be registered on the global `mcp = FastMCP("nora")`. It SHALL build a frozen `Device` via `DeviceResolver.build(host, "v2c", SnmpCredentials(community=community))`; when `validate=True`, it SHALL perform a cheap `sysDescr` GET against OID `1.3.6.1.2.1.1.1.0`; on success the device is inserted into `MutableInventory` and a `DeviceRecord` (with `SecretStr` credentials masked) is returned. New typed errors `DeviceUnreachable` (wire failure / `OSError`), `InvalidCommunity` (auth rejection / `puresnmp.exc.SnmpError`), and `InvalidHostError` (malformed host at Pydantic boundary) SHALL be raised on failure; on any failure, NO row is inserted.

#### Scenario: register_device with validate=True succeeds on reachable radio

- GIVEN a fake `SnmpClient` whose `get_oid("1.3.6.1.2.1.1.1.0")` returns `b"Cambium PMP 450i ..."`
- WHEN `register_device(host="192.0.2.10", community="MEXI2-BB-RW", validate=True)` runs
- THEN a typed `DeviceRecord` is returned AND `MutableInventory.get(device_id)` returns the device AND a subsequent `snmp_get_ap_summary(device_id=<returned>)` succeeds

#### Scenario: register_device with validate=True raises DeviceUnreachable when sysDescr fails

- GIVEN a fake `SnmpClient` whose `get_oid("1.3.6.1.2.1.1.1.0")` raises `OSError`
- WHEN `register_device(host="192.0.2.10", community="MEXI2-BB-RW", validate=True)` runs
- THEN `DeviceUnreachable("192.0.2.10")` is raised AND `MutableInventory.device_ids` remains empty

#### Scenario: register_device with validate=False inserts unconditionally

- GIVEN `validate=False` AND zero wire frames expected
- WHEN `register_device(host="192.0.2.10", community="MEXI2-BB-RW", validate=False)` runs
- THEN the device is inserted AND `MutableInventory.get(device_id)` returns it AND zero SNMP frames were sent

#### Scenario: register_device rejects malformed host with InvalidHostError

- GIVEN `host="not-an-ip"`
- WHEN `register_device(host="not-an-ip", community="MEXI2-BB-RW")` runs
- THEN `InvalidHostError("not-an-ip")` is raised AND no wire frame is sent AND `MutableInventory.device_ids` is unchanged

#### Scenario: register_device rejects invalid community with InvalidCommunity

- GIVEN a fake `SnmpClient` whose `get_oid("1.3.6.1.2.1.1.1.0")` raises `puresnmp.exc.SnmpError`
- WHEN `register_device(host="192.0.2.10", community="bogus-community", validate=True)` runs
- THEN `InvalidCommunity("bogus-community")` is raised AND `MutableInventory.device_ids` remains empty

#### Scenario: register_device idempotent on duplicate host returns distinct device_ids

- GIVEN two consecutive `register_device(host="192.0.2.10", community="MEXI2-BB-RW", validate=False)` calls
- WHEN both calls complete
- THEN the two returned `device_id`s differ (via `secrets.token_hex(3)` stem) AND both devices are reachable via `MutableInventory.get(...)`

#### Scenario: register_device is present in tools/list over stdio

- GIVEN the server booted via `mcp.run()` over stdio
- WHEN a client calls `tools/list`
- THEN the response contains `"register_device"` AND its `inputSchema` declares `host: str`, `community: str`, `validate: bool` (default `true`) AND the return shape matches `DeviceRecord.model_json_schema()`

### Requirement: Orchestrator Prompt Fallback For Ad-Hoc IPv4

`src/nora/prompts/netops_orchestrator.md` §4 Step 4 SHALL contain a fallback clause instructing the orchestrator: when `device_id` looks like an IPv4 literal AND `Inventory.get` raises `DeviceNotFoundError`, the orchestrator calls `register_device(host=<ipv4>, community=<operator-provided>)` instead of asking for a `device_id`. The clause SHALL reference the reachability validation contract (`sysDescr` GET) and SHALL NOT instruct the orchestrator to echo the community string back to the operator. (Test method: regex match against `src/nora/prompts/netops_orchestrator.md` file content; prompt tests are content-based, not behavioural.)

#### Scenario: prompt contains the register_device fallback clause

- GIVEN `src/nora/prompts/netops_orchestrator.md` content
- WHEN a regex scan looks for the literal `register_device` within §4 (between the `## 4.` and `## 5.` headings)
- THEN at least one match exists

#### Scenario: prompt clause references the reachability validation contract

- GIVEN the §4 fallback clause
- WHEN a regex scan looks for `sysDescr` OR `1.3.6.1.2.1.1.1.0` within §4
- THEN at least one match exists

#### Scenario: prompt clause does NOT instruct the orchestrator to leak the community string

- GIVEN the §4 fallback clause content
- WHEN a regex scan looks for `echo` followed by `community` within §4 (case-insensitive)
- THEN zero matches exist

### Requirement: `data/devices.yaml` Gitignore

`.gitignore` SHALL contain patterns preventing `data/devices.yaml` and `data/devices-*.yaml` from being committed while preserving `data/devices.example.yaml` as the tracked template. The stale "gitignored" claim in `data/devices.example.yaml:5` SHALL be removed.

#### Scenario: data/devices.yaml is gitignored

- GIVEN `.gitignore` containing `data/devices.yaml`, `data/devices-*.yaml`, and `!data/devices.example.yaml`
- WHEN `git check-ignore -v data/devices.yaml` runs
- THEN exit code is `0` AND a matching `.gitignore` line is named

#### Scenario: data/devices.example.yaml remains tracked

- GIVEN the same `.gitignore`
- WHEN `git check-ignore -v data/devices.example.yaml` runs
- THEN exit code is `1` (NOT ignored)

#### Scenario: stale gitignore claim is removed from devices.example.yaml

- GIVEN `data/devices.example.yaml`
- WHEN line 5 is inspected
- THEN it does NOT contain the substring `gitignored`
