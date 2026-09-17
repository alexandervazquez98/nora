# Delta for ad-hoc-device-registration

> Added by `2026-09-15-register-device-mcp` — closes issue #42 (IPv4-literal resolution gap). Defines a new capability covering ad-hoc device registration: the `MutableInventory` wrapper (preserves `Inventory.frozen=True`), the `register_device` MCP tool (`host` + `community` + `validate=True` reachability check against `sysDescr`), the §4 Step 4 orchestrator-prompt fallback for ad-hoc IPv4 literals, and the `.gitignore` policy for `data/devices.yaml`.


## ADDED Requirements

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
