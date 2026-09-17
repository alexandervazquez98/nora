# Proposal: register_device MCP tool + orchestrator prompt update for ad-hoc IP resolution

**Status**: proposed. Single PR to `main`. Closes #42.

## Why

Issue #42 blocks operators when an IPv4 literal is absent from `data/devices.yaml` — `Inventory.get()` raises `DeviceNotFoundError` and the orchestrator stops (see issue body: the `10.53.20.5` reproduction). Three underlying gaps:

1. **`DeviceResolver.build(...)` (#14) was never wired into the runtime** — the driver still routes every lookup through `Inventory.get()` (explore §2.2).
2. **The "Device Abstraction" rule quoted in the issue is not in `netops_orchestrator.md`** — the closest rule is §1 Zero-Leakage & Privacy Contract; the orchestrator has no fallback clause for IPv4 misses (explore §4.1).
3. **`data/devices.yaml` is not gitignored** despite `devices.example.yaml:5` claiming it is — security debt that must land even with v1 in-memory (explore §5.2).

## What changes

| Area | Behaviour |
|------|-----------|
| New `@mcp.tool register_device(host, community, validate=True) -> DeviceRecord` | Builds a frozen `Device` via `DeviceResolver.build(...)`. `validate=True` performs a cheap `sysDescr` GET (`1.3.6.1.2.1.1.1.0`). On success returns a `DeviceRecord` with `SecretStr` credentials masked. On wire failure raises a typed error and inserts nothing. |
| New `MutableInventory` wrapper | Holds a `dict[str, Device]` and a read-through reference to the frozen `Inventory`. Exposes `register(device)` / `unregister(device_id)` / `get(device_id)` / `device_ids`. `Inventory.frozen=True` is preserved. The driver layer routes through the wrapper. |
| Prompt §4 Step 4 fallback | New clause: when `device_id` looks like IPv4 and `Inventory.get()` raises `DeviceNotFoundError`, the orchestrator calls `register_device(host, community)` with the operator-provided community instead of asking for a `device_id`. Matches §1 Zero-Leakage tone. |
| Catalog re-sign | PMP 450i baselines `15.2.1`, `15.3.0`, `25.1.0`: add `"sysDescr": "1.3.6.1.2.1.1.1.0"` to `oids`, add `"register_device": ["sysDescr"]` to `tools`, HMAC re-sign with `scripts/sign_catalog.py`. |
| Allow-list cleanup | Remove `snmp_get_pmp450i_radio_metrics` from `_ALLOWED_UNCATALOGUED_TOOLS` (`src/nora/server.py:626`), bundled with the same re-sign (archive-report §149-151, verify-report §372). |
| `.gitignore` | Add `data/devices.yaml` + `data/devices-*.yaml` (with `!data/devices.example.yaml` exception); fix the stale comment in `devices.example.yaml:5`. |
| Test surface | New `tests/test_register_device.py` (~120 LoC); `test_inventory.py` additions for `MutableInventory`; rename `test_server_exposes_exactly_eleven_tools` → `_twelve_tools`; `test_oid_catalog_integration.py` + `tests/conftest.py` updates for the catalog re-sign. |

### Capabilities (sdd-spec contract)

- **New**: `ad-hoc-device-registration` — `register_device` MCP tool + `MutableInventory` wrapper.
- **Modified**: `driver-snmp-pmp450i` (catalog envelope adds `register_device`); `nora-mcp-server` (12 tools); `prompts` (netops_orchestrator §4 Step 4 fallback).

## What does NOT change

11 existing tools' signatures and behaviour; `Inventory.frozen=True` invariant; operator's `data/devices.yaml` boot-load workflow; YAML persistence semantics (runtime mutations stay in-memory); the 6 other tools absent from §4 of the prompt (deferred to a separate change); discovery protocols; firmware pinning; batch import.

## Trade-offs and alternatives considered

| Option | Verdict | Why |
|--------|---------|-----|
| A: Auto-resolver + default community | Rejected | Silent auto-resolution bypasses audit/governance; default credential leaks into LLM context. |
| B: `register_device` without prompt update | Rejected | Pure tool without prompt fallback leaves the orchestrator stuck in the original failure mode. |
| C: Optional `community` on diagnostic tools | Rejected | Leaks credentials into model context, violating §1 Zero-Leakage. |
| **D: hybrid (this proposal)** | **Accepted** | Operator-driven registration, prompt fallback, masked credentials, in-memory scope, gitignore fix. |

`MutableInventory` cost: ~80 LoC of wrapper + tests to preserve the frozen invariant. Mutating `Inventory` directly would break ~217 lines of existing tests (explore §2.1).

## Success criteria

- `register_device("10.53.20.5", "MEXI2-BB-RW")` returns a `DeviceRecord` when sysDescr GET succeeds.
- `register_device` with `validate=True` raises `DeviceUnreachable` (or typed equivalent) on sysDescr miss; no row inserted.
- `register_device` with `validate=False` inserts unconditionally.
- Post-registration, the existing `snmp_get_ap_summary(device_id="10.53.20.5")` succeeds.
- Prompt clause present in `netops_orchestrator.md`, matches §1 Zero-Leakage tone.
- HMAC verification still passes for all 3 PMP 450i baselines post re-sign.
- `data/devices.yaml` is gitignored.
- All existing tests green; new tests cover the new behaviour (strict TDD per `openspec/config.yaml::testing.strict_tdd=true`).
- `_ALLOWED_UNCATALOGUED_TOOLS` no longer contains `snmp_get_pmp450i_radio_metrics`.

## Risks (ranked)

1. **`MutableInventory` wrapper surface drift** — every read site that currently calls `Inventory.get()` must route through the wrapper; a missed call site breaks runtime resolution silently. Mitigation: exhaustive grep for `self._inventory.get` and `Inventory(...).get`; new regression test pins the path.
2. **Catalog re-sign is operator-side** — `scripts/sign_catalog.py` + HMAC key rotation is manual; a botched re-sign invalidates all 3 baselines and breaks boot. Mitigation: re-verify with `OidCatalogRegistry.verify_all` immediately after re-sign, inline in the same PR.
3. **Prompt clause is heuristic** — §4 guidance may be bypassed under edge cases; the prompt test surface is conversational, not deterministic. Mitigation: explicit "when in doubt, ask operator for community" fallback in the clause.

## Out of scope (re-affirm)

YAML persistence of runtime-registered devices; hot-reload of `data/devices.yaml`; resyncing the 6 other tools into §4 of the prompt; discovery protocols (CDP/LLDP/DHCP snooping); firmware pinning; batch import; GUI inventory editor; `NORA_DEFAULT_SNMP_COMMUNITY` env var; optional `community` parameter on existing diagnostic tools.

## Rollout

Single PR to `main`. Strict TDD. ~280 LoC production + ~140 LoC new tests. PR body `Closes #42`. Catalog re-sign + HMAC re-verify inline in the same PR; no follow-up. `_ALLOWED_UNCATALOGUED_TOOLS` cleanup bundled with the same re-sign.