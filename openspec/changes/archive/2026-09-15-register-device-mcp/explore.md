# Explore — `register_device` MCP Tool + Orchestrator Prompt Update (Issue #42)

> Scope: investigate the codebase ahead of proposing a change that resolves the
> ad-hoc IP resolution gap surfaced in production (`Inventory.get()` raises
> `DeviceNotFoundError` when an operator hands the orchestrator an IPv4
> literal that is absent from `data/devices.yaml`). The architectural
> direction is fixed by the user: **new MCP tool `register_device` plus a
> prompt update that teaches the orchestrator to call it on IPv4 misses**.
> Persistence of runtime-registered devices to `data/devices.yaml` is a
> separate question (left as an open question for the design phase).

---

## 1. Surface Analysis

Files that will be touched in the change (mark `existing` / `new`, LoC
delta is rough and only used to verify the 800-line review budget).

| File | State | LoC Δ | Purpose |
|------|-------|-------|---------|
| `src/nora/drivers/inventory.py` | existing | +30 | Add mutable mutator to `Inventory` (or a separate `MutableInventory` wrapper). Today the model is `ConfigDict(frozen=True)` with only `from_yaml` / `get` / `device_ids` (lines 80–130). |
| `src/nora/drivers/resolver.py` | existing | +25 | Reuse `DeviceResolver.build(...)` from `register_device` to package `host + community → Device`. No new methods required; possible thin helper for the catalog-resolve step. |
| `src/nora/drivers/exceptions.py` | existing | +20 | Add `DeviceUnreachable` and `InvalidCommunity` typed errors under `DriverError`. Existing `NetworkUnreachableError` / `SnmpTimeoutError` cover the wire-failure cases but cannot distinguish "host closed" from "auth rejected" — a distinction the validate path needs. |
| `src/nora/server.py` | existing | +70 | Register `@mcp.tool register_device(host, community, validate=True)`. Add `"register_device"` to `__all__` and to `_ALLOWED_UNCATALOGUED_TOOLS` (see risk #1) OR remove the entry once the catalog re-sign lands. Update the stale module docstring (currently claims "five `@mcp.tool` registrations" — already drifted past that to 11). |
| `src/nora/cli.py` | existing | +5 | Wire the new tool's boot-time expectations: nothing structural; the `verify_tools_are_catalogued(...)` call already enforces catalog membership. |
| `src/nora/prompts/netops_orchestrator.md` | existing | +15 | Add a Step 0 / Step 4.5 that instructs the orchestrator: when `device_id` looks like an IPv4 and `Inventory.get` raises `DeviceNotFoundError`, call `register_device(host, community)` (community comes from the operator in the chat). The prompt's current tool-list (Section 4) is inline — no separate tool catalog. |
| `data/oid-catalogs/cambium/pmp450i/{15.2.1,15.3.0,25.1.0}.json` | existing | +6 | Add `"sysDescr": "1.3.6.1.2.1.1.1.0"` to the `oids` map and `"register_device": ["sysDescr"]` to the `tools` envelope. **Requires HMAC re-sign** with the operator key (`scripts/sign_catalog.py`); the `hmac_sha256` field is verified at boot. |
| `tests/test_inventory.py` | existing | +40 | New failing tests: `test_inventory_register_adds_device`, `test_inventory_register_rejects_duplicate_id`, `test_inventory_register_overrides_frozen_model`. |
| `tests/test_register_device.py` | new | +120 | TDD surface for `register_device`: success with `validate=True` returns typed `DeviceRecord`; rejects unreachable host; rejects invalid community; never inserts on miss (validate must be cheap and side-effect-free on failure); operator-provided community is masked on the returned payload. |
| `tests/test_server.py` | existing | +15 | Update `test_server_exposes_exactly_eleven_tools` → `_twelve_tools` and the `expected` set. |
| `tests/test_oid_catalog_integration.py` | existing | +25 | Add `"register_device": ["sysDescr"]` to `_INTEGRATION_CATALOG_TOOLS` and re-sign the hermetic fixture (the existing `sample_catalog` fixture in `tests/conftest.py` uses the canonical HMAC key, so a fresh re-sign is required). |
| `tests/test_resolver.py` | existing | +10 | Triangulation: `DeviceResolver.build` continues to never mutate inventory (the test `test_build_does_not_open_devices_yaml` already pins this — keep it green). |
| `openspec/changes/2026-09-15-register-device-mcp/{proposal,design,specs,tasks}.md` | new | n/a | Created in subsequent phases. |
| `openspec/specs/{driver-snmp-pmp450i,prompts}/spec.md` | existing | +20 | Add the `register_device` requirement + the prompt-update requirement as MODIFIED deltas. |

**Estimated total LoC delta**: ~+400 lines (excluding new tests). Well under the
800-line review budget. The dominant deltas are the new
`tests/test_register_device.py` (~120), the catalog re-signs (~+6 across three
files), the prompt update (+15), and the new MCP tool wrapper (+70).

---

## 2. Codebase Evidence

### 2.1 Inventory mutability — **NO mutator exists today**

`Inventory` is a `BaseModel` with `model_config = ConfigDict(frozen=True)`
(`src/nora/drivers/inventory.py:83`). The full public surface is:

- `Inventory.from_yaml(path: Path) -> Inventory` (line 92)
- `Inventory.get(device_id: str) -> Device` (line 121) — raises `DeviceNotFoundError`
- `Inventory.device_ids -> list[str]` (line 129)

There is **no** `add`, `register`, `upsert`, `update`, or `__setitem__`. The
frozen model is intentional — every existing test relies on the
"once-loaded-from-YAML, never-mutated" contract (e.g.
`tests/test_inventory.py::test_inventory_loads_single_device`,
`tests/test_driver_snmp_pmp450i.py` back-compat suite).

**Recommendation for the design phase**: do NOT remove `frozen=True`. Two
clean options, in order of preference:

1. **`MutableInventory` wrapper** (preferred) — a thin class that holds a
   `dict[str, Device]` and delegates `get` / `device_ids` to it. Boot
   constructs `MutableInventory` from `Inventory.from_yaml(...)`; the
   driver layer continues to receive a `Protocol Inventory`-shaped facade
   (`InventoryLike`). `register_device` mutates the wrapper; the YAML
   inventory remains frozen and immutable.
2. **Replace `self._inventory` on the driver at runtime** — swap the
   whole attribute after registering. Less clean: every read path goes
   through `self._inventory.get(...)`, so an attribute swap needs to be
   thread-safe (the FastMCP tool calls can interleave under async).

Option 1 keeps the `frozen=True` guarantee intact and matches the
existing pattern (the prompt registry is also wrapped behind a frozen
`PromptRegistry` returned from a mutable scanner).

### 2.2 DeviceResolver integration

`DeviceResolver` (`src/nora/drivers/resolver.py`) is a pure factory:
`build(host, snmp_version, creds) -> Device`. It does **no I/O** (test
`test_build_does_not_open_devices_yaml` pins this) and produces a frozen
`Device` whose `device_id` is `f"adhoc-{host}-{secrets.token_hex(3)}"` —
collision-safe per process but NOT persisted.

**Composition recommendation**: `register_device` calls
`DeviceResolver.build(host, "v2c", SnmpCredentials(community=...))` (v2c is
the path the issue body describes — "comunidad MEXI2-BB-RO"), then runs a
cheap `sysDescr` GET against the resulting `Device.host`, then inserts the
frozen `Device` into the `MutableInventory`. The resolver stays a pure
factory; the wire validation and the inventory mutation live in the
`register_device` body.

The Pydantic `Device` model already validates v2c-requires-community
(line 58) so the wrapper catches malformed creds before any wire frame.

### 2.3 MCP tool registration

Tools live in **`src/nora/server.py`** (NOT `src/nora/drivers/snmp_pmp450i/server.py` —
that path does not exist; the user's preflight referenced the wrong file).
The eleven current tools are decorated `@mcp.tool` at lines 154, 185, 206,
235, 261, 293, 330, 371, 410, 433, 469.

Two `@mcp.prompt` registrations: `netops_orchestrator` (line 505) and
`snmp_pmp450i` (line 511).

**Catalog guard** (`server.py:656-703`): every `@mcp.tool` must satisfy
ONE of:

- Present in `OidCatalogRegistry.REQUIRED_OIDS_BY_TOOL[(vendor, model)]`,
  OR
- Listed in `_ALLOWED_UNCATALOGUED_TOOLS` (line 626).

The current allow-list has 5 entries:

```python
_ALLOWED_UNCATALOGUED_TOOLS: frozenset[str] = frozenset({
    "search_intervention_history",
    "get_device_lifecycle_summary",
    "correlate_sector_interference",
    "save_intervention_record",
    "snmp_get_pmp450i_radio_metrics",
})
```

The last entry (`snmp_get_pmp450i_radio_metrics`) is documented tech debt
(archive-report §149-151 + verify-report §372): awaiting a catalog
re-sign to remove it.

**Two paths for `register_device`**:

- **Path A (catalog-aligned, preferred)** — extend the catalog
  `oids` map with `sysDescr: "1.3.6.1.2.1.1.1.0"` and the `tools`
  envelope with `"register_device": ["sysDescr"]`, then re-sign the
  three baseline catalogs (`15.2.1`, `15.3.0`, `25.1.0`) with
  `scripts/sign_catalog.py`. The guard accepts the new tool. **This
  also fixes the outstanding `snmp_get_pmp450i_radio_metrics`
  allow-list debt** if the same re-sign adds that tool's envelope.
- **Path B (allow-list, faster)** — add `"register_device"` to
  `_ALLOWED_UNCATALOGUED_TOOLS`. Faster but accumulates tech debt;
  the verify-report already flagged this pattern as something to
  retire.

**Recommendation**: Path A. Re-signing is mechanical (`scripts/sign_catalog.py`
already exists; `tests/conftest.py` shows the canonical HMAC path). The
combined re-sign also closes the outstanding debt.

### 2.4 Inventory persistence — runtime-only for v1

Where `devices.yaml` is loaded:

- `Settings.nora_devices_inventory_path` defaults to
  `./data/devices.yaml` (`src/nora/config.py:51`).
- Loaded once at boot via `Inventory.from_yaml(...)` in
  `src/nora/cli.py:237`.

**The runtime NEVER reloads it after boot.** The prompt registry has the
same one-shot semantic (PromptRegistry scans once, "Hot-reload is
forbidden by spec" — `src/nora/prompts/registry.py:55-57`). Hot-reload is
explicitly out of scope per spec.

**Persistence story for runtime-registered devices** (left as open
question #1 below): in-memory only for v1; the device is forgotten when
`nora-mcp.service` restarts. Operators who want durable registration
either edit `data/devices.yaml` and restart, or accept the ephemeral
session-registration model. This matches the orchestrator's natural
pattern (each chat session gets a fresh inventory view).

**`.gitignore` check** (this is a non-obvious finding):

```
$ git check-ignore -v data/devices.yaml
data/devices.yaml
not ignored
```

`data/devices.yaml` is **NOT gitignored today**. `.env` IS gitignored
(`src/nora/config.py:42` loads `.env` as the dotenv file; the pattern
`.env` in `.gitignore` covers it). The README of
`data/devices.example.yaml` claims "NEVER commit `data/devices.yaml`;
it is gitignored" (line 5) — that claim is **currently false** and must
be fixed in this change (add `data/devices.yaml` to `.gitignore` AND
remove the false claim from `devices.example.yaml`).

---

## 3. Test Surface

### 3.1 Test directory layout

`tests/` (flat layout for the driver layer; sub-packages for
intervention_memory and intervention_writer):

```
tests/
├── test_resolver.py             # existing (DeviceResolver tests)
├── test_inventory.py            # existing (Inventory + Device model)
├── test_server.py               # existing (FastMCP tool surface)
├── test_oid_catalog.py          # existing (catalog loader / HMAC)
├── test_oid_catalog_integration.py  # existing (catalog guard E2E)
├── test_prompts.py              # existing (PromptRegistry)
├── test_snmp_subscribers.py      # slice 3 SM table (mock-based)
├── test_snmp_summaries.py       # slice 2 read summaries (mock-based)
├── test_snmp_spectrum.py        # slice 4 spectrum
├── test_snmp_migrate.py         # slice 4 HITL migration
├── test_driver_snmp450i_readonly.py  # readonly enforcement (AST lint)
├── conftest.py                  # shared fixtures
│   ├── sample_catalog           # signed catalog fixture
│   ├── sample_inventory         # 2-device YAML (v2c + v3)
│   ├── hermetic_settings        # fresh Settings bound to tmp_path
│   ├── tmp_catalogs_dir         # hermetic operator root
│   └── tmp_builtin_root         # hermetic built-in root
├── intervention_memory/         # sub-package (own fixtures)
└── intervention_writer/         # sub-package (own fixtures)
```

### 3.2 SNMP test pattern

Tests use **mock clients**, not real radios. The pattern (visible in
`test_snmp_summaries.py` and `test_driver_snmp_pmp450i.py`):

1. Inject a fake `SnmpClient` via `Pmp450iDriver(inventory=..., catalog_registry=..., client_factory=lambda dev: fake_client)`.
2. The fake returns canned `(oid → value)` maps; specific tests monkey-patch
   `client.get_oid` to raise `TimeoutError`, `OSError`, etc. to exercise the
   typed-exception paths.
3. Real-radio tests (`test_driver_snmpsim_v2c.py`, `test_driver_snmpsim_v3.py`)
   use the `snmpsim` package, which is opt-in.

For `register_device`, the same pattern applies: inject a fake client
that returns a canned `sysDescr` string for the success path, raises
`OSError` for the unreachable-host path, and raises `SnmpError` (the
`puresnmp.exc.SnmpError`) for the invalid-community path.

### 3.3 Strict TDD — NEW failing tests required

Per `obs #12940` (Strict TDD active) and the `openspec/config.yaml::testing.strict_tdd=true`, every new behavior lands a failing test first.

**Required new tests for `tests/test_register_device.py` (NEW FILE)**:

| Test | Scenario |
|------|----------|
| `test_register_device_success_with_validate` | `validate=True` issues a `sysDescr` GET, parses the result, inserts the device, returns a typed `DeviceRecord` with masked credentials. |
| `test_register_device_success_without_validate` | `validate=False` skips the wire GET; returns the `DeviceRecord` from `DeviceResolver.build(...)` immediately. |
| `test_register_device_rejects_unreachable_host` | Fake client raises `OSError` → `DeviceUnreachable` raised, **no** row inserted (verify with a follow-up `inventory.get(device_id)` that raises `DeviceNotFoundError`). |
| `test_register_device_rejects_invalid_community` | Fake client raises `puresnmp.exc.SnmpError` → `InvalidCommunity` raised, no insert. |
| `test_register_device_idempotent_on_duplicate_host` | Two consecutive `register_device(host, community)` calls for the same IP return two distinct `device_id`s (the `secrets.token_hex(3)` stem guarantees this; pin the assertion). |
| `test_register_device_payload_masks_credentials` | The returned `DeviceRecord.model_dump(mode="json")` contains `"**********"` for `community`; the literal community string is absent (Zero-Leakage). |

**Required new tests for `tests/test_inventory.py` (EXISTING)**:

| Test | Scenario |
|------|----------|
| `test_inventory_register_adds_device` | `MutableInventory.register(Device(...))` makes the device reachable via `.get(...)`. |
| `test_inventory_register_overrides_loaded_yaml` | A `MutableInventory` seeded from `Inventory.from_yaml` allows runtime-registered entries to coexist with YAML-loaded entries. |
| `test_inventory_register_rejects_duplicate_id` | Registering a device whose `device_id` already exists raises a typed error (likely `DeviceAlreadyRegisteredError`, a new sibling to `DeviceNotFoundError`). |

**Required updates to `tests/test_server.py`**:

- `test_server_exposes_exactly_eleven_tools` → `test_server_exposes_exactly_twelve_tools`, with `register_device` in the expected set.

**Required updates to `tests/test_oid_catalog_integration.py`**:

- Add `"register_device": ["sysDescr"]` to `_INTEGRATION_CATALOG_TOOLS` (the test's hermetic catalog fixture).
- Re-sign with the canonical HMAC key (the conftest fixture already does this for the existing entries — extend the dict).

**Required updates to `tests/conftest.py`**:

- Add a `sysDescr` entry to `_SAMPLE_CATALOG_PAYLOAD` so hermetic catalogs that drive `register_device` tests have the OID defined.

---

## 4. Prompt Contract

### 4.1 Current "Device Abstraction" wording — **does NOT exist verbatim**

The issue body quotes:

> *"Device Abstraction: Network hardware is addressed via device_id (or IP when permitted by inventory). Never require or leak raw physical credentials or community strings."*

**This rule is NOT in the current `src/nora/prompts/netops_orchestrator.md`** (verified via `grep -r "Device Abstraction"` — zero matches in `openspec/` or `src/nora/`). The closest rule in the prompt today is the **§1 Zero-Leakage & Privacy Contract** (lines 10–14):

```markdown
## 1. Zero-Leakage & Privacy Contract
You MUST NOT echo raw sensitive credentials or infrastructure details:
- Use RFC 5737 / RFC 1918 generic documentation formats for examples (e.g. 192.0.2.10, 198.51.100.1).
- Refer to towers and sectors by logical aliases (e.g. TOWER_ALPHA, SECTOR_01).
- Never expose raw SNMP read/write community strings or user credentials in visible chat responses.
```

This means the proposed prompt edit is **not "modify an existing rule"** — it
is "add a new rule". The "Device Abstraction" wording in the issue is the
*aspirational* state after the change. The design phase should decide
whether to:

- Adopt the exact wording from the issue (preferred — it is the operator's
  stated contract), OR
- Phrase the rule using the prompt's existing style (Spanish-allowed,
  numbered, RFC-aligned).

### 4.2 Where the orchestrator learns tool names

There is **no separate tool-catalog reference**. The orchestrator prompt
lists each MCP tool inline as part of the operational sequence. Section
§4 (lines 34–53) currently enumerates five tools in numbered steps:

```markdown
## 4. Intervention Memory & NORA MCP Tools Protocol
You have access to NORA MCP tools. Follow this operational sequence:
1. Step 1: Check Lifecycle & Pre-existing State:
   get_device_lifecycle_summary(target_ip="<target_ip>")
2. Step 2: Historical Detail Search:
   search_intervention_history(target_ip="<target_ip>", stage="PRE_DIAGNOSTIC")
3. Step 3: Sector Interference Correlation:
   correlate_sector_interference(tower_name="<tower_name>", target_frequency_mhz=<freq>)
4. Step 4: Radio Metrics Telemetry:
   snmp_get_pmp450i_radio_metrics(device_id="<device_id>")
5. Step 5: Persist a New Intervention Record:
   save_intervention_record(payload={...})
```

(Slice 2/3/4 added five more tools in the actual FastMCP surface but did
NOT add them to the prompt — the orchestrator only knows about the five
listed above. This is a separate drift from the 11-tool surface that
should be flagged to the operator but is out of scope for issue #42.)

The new `register_device` should be added to §4 as a **Step 0** ("before
invoking any telemetry tool, if the device is unknown, register it") or
as a fallback clause inside Step 4 ("if `Inventory.get` raises, call
`register_device` with the operator-provided community"). The latter is
cleaner and matches the orchestrator's existing pattern of injecting
recovery into the operational sequence.

---

## 5. Security Considerations

### 5.1 Community string flow

The strategy fixed by the user: **operator → chat → tool argument →
SNMP**. The community does NOT come from `.env`. This avoids the
auto-fallback concern from issue #42 ("Option A — auto-resolver with
default community in MCP") leaking a default credential into LLM
context.

Concrete flow:

1. Operator types in chat: *"Diagnostica el AP 10.53.20.5 con la
   comunidad MEXI2-BB-RO."*
2. Orchestrator reads the message, identifies the IP and community.
3. Orchestrator calls `register_device(host="10.53.20.5",
   community="MEXI2-BB-RO")` — the community is a tool argument, so it
   IS visible to the model on the way in (acceptable; the operator
   just provided it).
4. `register_device` wraps it in `SecretStr`, validates, inserts.
5. Subsequent `snmp_get_*` calls resolve the device by `device_id`,
   **not** by community; the community never re-enters the chat.

**Returning the `DeviceRecord` to the orchestrator**: the tool MUST use
`device.model_dump(mode="json")` (which masks `SecretStr` to
`"**********"`) so the community string is masked in the wire response.
The existing telemetry-sanitizer layer (`src/nora/sanitizer.py:13-30`) does
NOT mask credentials — its docstring is explicit: "API keys / tokens /
credentials — handled by `secure-configuration` upstream." `SecretStr`
masking at the Pydantic boundary is the contract that holds this line.

### 5.2 `data/devices.yaml` gitignore status — **INCORRECT**

**Finding**: `data/devices.yaml` is NOT in `.gitignore` today
(`git check-ignore -v data/devices.yaml` → "not ignored"). The comment
in `data/devices.example.yaml:5` claims the file is gitignored — that
claim is currently **false**. If `register_device` ever persisted
credentials to `data/devices.yaml`, an accidental `git add data/` would
commit plaintext community strings.

**Mandatory fix** (this change): add `data/devices.yaml` to `.gitignore`.
Even if v1 stays in-memory-only, the operator-facing contract already
implies YAML persistence as the recovery path, so the gitignore must
arrive now.

The recommended pattern in `.gitignore`:

```gitignore
# Operator-supplied device inventory (contains SNMP credentials).
data/devices.yaml
data/devices-*.yaml
!data/devices.example.yaml
```

### 5.3 Sanitizer — no changes needed

`Sanitizer.sanitize(...)` (`src/nora/sanitizer.py`) handles four
identifier categories (private IPv4, MAC, serial, hostname). The
`register_device` payload's only sensitive fields are credentials
(`SecretStr`-masked at the Pydantic boundary) and the host IP (RFC 5737
in tests; private in production, already sanitized by the existing
infra — same masking rules that the rest of the codebase already
applies). **No sanitizer changes required**.

### 5.4 Audit trail

There is **no structured audit channel** today. The closest pattern is
the `_ToolLogMiddleware` (`src/nora/server.py:522-541`) which emits one
structured stderr line per `@mcp.tool` call (`tool=<name>
duration_ms=<int> outcome=<success|error>`). That handles observability
for the tool surface.

For `register_device`, the existing middleware already gives us:

```
tool=register_device duration_ms=234 outcome=success
```

…which is sufficient for operational audit (it shows the tool ran and
its outcome, not the credentials). The driver layer's
`logger.debug("driver registry injected: %s", type(driver).__name__)`
pattern (`src/nora/drivers/registry.py:49`) is the model to follow for
runtime-mutated state — emit one structured `logger.info` line per
`register_device` call so the operator can correlate.

**No new audit channel is needed**. The thin server middleware is enough.

---

## 6. Out-of-Scope (for this change)

Explicitly deferred (do not propose them in this delta):

- **Discovery protocols** — CDP/LLDP/DHCP snooping. The orchestrator
  hands `register_device` an IP; we don't auto-discover.
- **Firmware pinning** — the ad-hoc device ships `firmware="(adhoc)"`
  (matches `DeviceResolver.build(...)` placeholder); the next telemetry
  call (`report_firmware`) overwrites the field at runtime. A separate
  change can lift that flow into `register_device`.
- **Batch import** — one device per `register_device` call; no
  CSV/bulk-register.
- **Multi-device transactions** — no atomic all-or-nothing semantics.
  Each `register_device` is independent.
- **GUI inventory editor** — operators edit `data/devices.yaml`
  directly or via the MCP tool. No web UI.
- **Persistence to `data/devices.yaml`** — deferred to a follow-up (see
  open question #1).
- **Auto-reload of `data/devices.yaml`** — out of scope; the prompt and
  inventory registries are both one-shot at boot per spec.
- **`NORA_DEFAULT_SNMP_COMMUNITY`** env var — explicitly rejected. The
  community flows operator → chat → tool argument, never env.
- **Optional `community` parameter on existing diagnostic tools** —
  explicitly rejected (would leak credentials into model context).

---

## 7. Risk Callouts

### 7.1 Catalog guard — must accept the new tool or re-sign

`_ALLOWED_UNCATALOGUED_TOOLS` (`src/nora/server.py:626`) is currently 5
entries; adding `"register_device"` is the **fastest** path but
perpetuates the tech debt the previous archive flagged
(`openspec/changes/archive/2026-09-13-pmp450i-production-surface/archive-report.md:149-151`).

The clean path is to **re-sign the three baseline catalogs** with the
`sysDescr` OID + `register_device` envelope entry, AND remove the
existing `snmp_get_pmp450i_radio_metrics` allow-list entry (the same
re-sign can include its tool envelope). This collapses two pieces of tech
debt in one catalog update.

If the catalog re-sign is deferred, the boot will fail closed:
`verify_tools_are_catalogued` raises `UncataloguedToolError` and the
service never reaches `mcp.run()`.

### 7.2 Test that pins the 11-tool surface

`tests/test_server.py::test_server_exposes_exactly_eleven_tools` will
fail on the FIRST test run. The fix is mechanical (rename to `_twelve`
and add `register_device` to `expected`) but must arrive in the SAME PR
that adds the new tool — otherwise `tests/test_server.py` blocks CI.

### 7.3 Frozen-`Inventory` invariant — back-compat suite

Every test in `tests/test_inventory.py` (217 lines) and the
`tests/test_driver_snmp_pmp450i.py` back-compat suite relies on
`Inventory` being frozen + loaded-once from YAML. Adding a mutator
either as a new method (breaks the Pydantic `frozen=True`) or as a
wrapper class (preserves the contract). **The wrapper-class approach
(section 2.1) is required** to keep these tests green.

### 7.4 Orchestrator prompt drift — pre-existing

Section §4 of `src/nora/prompts/netops_orchestrator.md` lists five tools.
The actual FastMCP surface has 11. This drift pre-dates issue #42 but
should be flagged to the operator. Adding `register_device` widens the
gap; the proposal phase should ask whether the operator wants the prompt
brought back into sync (touching more lines than the strict
"register_device-only" minimum).

### 7.5 Operator-side compatibility risk — `Inventory.get` error path

Today, an operator handing the orchestrator an unknown IP gets:

```
Error calling tool 'snmp_get_ap_summary': 10.53.20.5
(backend: DeviceNotFoundError: 10.53.20.5)
```

After this change, the same operator gets a prompt-instructed
`register_device` call followed by the diagnostic. Any operator scripts
that grep the server stderr for `DeviceNotFoundError` to detect "unknown
device" will still see it during `register_device` itself (when the
inventory truly does not have the device). The contract change is at the
orchestrator level, not the driver level — `Inventory.get` still raises
the same exception.

---

## 8. Open Questions (to resolve in design phase, NOT now)

1. **Persistence**: write runtime-registered devices back to
   `data/devices.yaml`, or stay in-memory only for v1? Memory-only is
   simpler and matches the prompt-registry "scan once" pattern; YAML
   persistence requires atomic-write semantics (`tmp + fsync +
   os.replace`, same pattern as `intervention_writer/atomic.py`) AND a
   gitignore fix (see §5.2).
2. **Hot-reload of `data/devices.yaml`** after a manual operator edit?
   Spec currently forbids it (one-shot at boot); would require a
   separate reload tool or SIGHUP handler.
3. **Should `register_device` accept `firmware` and `model` as optional
   hints?** Today's strategy pins `vendor="cambium"`, `model="pmp450i"`,
   `firmware="(adhoc)"` — the same placeholders `DeviceResolver.build`
   uses. A `validate=True` GET against `sysDescr` can extract the
   firmware at registration time (the parser `_parse_sysdescr_version`
   already exists at `src/nora/drivers/snmp_pmp450i/driver.py:180`).
   Should `register_device` populate the firmware field on success, or
   leave that to the next telemetry call?
4. **Error taxonomy**: do we need separate `DeviceUnreachable` and
   `InvalidCommunity` exceptions, or can `register_device` reuse the
   existing `NetworkUnreachableError` + a new `RefusesWriteError`-style
   credential error? The user's preflight mentioned both new exception
   names; the design phase should confirm.
5. **Should the orchestrator prompt be brought back into sync with the
   actual 11-tool surface as part of this change**, or scoped tightly to
   the `register_device` insertion?

---

## Architectural Snapshot

| Decision | Direction |
|----------|-----------|
| New MCP tool | `register_device(host, community, validate=True)` returns typed `DeviceRecord` |
| Inventory mutation | `MutableInventory` wrapper around frozen `Inventory` (keeps `frozen=True` invariant) |
| Validation wire frame | Cheap `sysDescr` GET (`1.3.6.1.2.1.1.1.0`) — RFC 1213, not vendor-specific |
| Tool-catalog alignment | Re-sign baseline catalogs (preferred) — closes existing tech debt in same PR |
| Persistence | In-memory only for v1; YAML persistence deferred (open question #1) |
| Prompt update | Add fallback clause inside §4 Step 4; do NOT add `community` parameter to existing tools |
| Audit | Reuse `_ToolLogMiddleware`; no new audit channel |
| `.gitignore` | Add `data/devices.yaml` (and the `data/devices-*.yaml` glob) — fix the pre-existing inconsistency |
| Tests | New `tests/test_register_device.py` + updates to `test_inventory.py`, `test_server.py`, `test_oid_catalog_integration.py`, `conftest.py` |

---

## Ready for Proposal

**Yes.** The architectural direction is fixed, the surface is mapped, the
tests are scoped, the risks are listed, and the open questions are
flagged for the design phase. The proposal phase should:

1. Write `openspec/changes/2026-09-15-register-device-mcp/proposal.md`
   with `## Intent` / `## Scope` / `## Capabilities` (New + Modified)
   mirroring the previous `2026-09-13-pmp450i-production-surface` proposal.
2. Resolve open question #4 (error taxonomy) before drafting specs —
   `DeviceUnreachable` / `InvalidCommunity` are mentioned in the
   preflight but not confirmed against the existing exception
   vocabulary.
3. Confirm with the operator whether to also retire the
   `_ALLOWED_UNCATALOGUED_TOOLS` `snmp_get_pmp450i_radio_metrics`
   entry in the same change (it costs one catalog re-sign and
   collapses two pieces of debt).