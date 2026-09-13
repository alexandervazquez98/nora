# Design: PMP 450i Production Surface

## Technical Approach

`DeviceDriverInterface` Protocol is the vendor seam; `Pmp450iSnmpDriver` adapts `Pmp450iDriver` (public surface unchanged). Six `@mcp.tool`s are thin protocol delegates. Slice 4 adds `hitl/tokens.py` (stub verifier + `threading.Timer` watchdog) and `Settings.nora_hitl_*`. Slice 5 hardens with a boot-time `@mcp.tool()` registration guard. `OidCatalogRegistry.resolve(...)` already emits the literal minor-mismatch warning; slices 2-5 extend the envelope with a `"tools"` map and per-tool index.

## Architecture Decisions

| # | Option | Tradeoff | Decision |
|---|--------|----------|----------|
| 1 | Stub HITL verifier | Full state machine is Phase 3; stub ships auditable seam. | **Stub.** Raises `AutonomousMutationRejected("...HITL approval token required")`. |
| 2 | Centralised `categorize_subscribers(...)` returning `Literal["ONLINE_ACTIVE","ACTIVE_DEGRADED","PRE_EXISTING_OFFLINE"]` | Ad-hoc drifts → false migration timeouts. | **Centralised.** |
| 3 | Per-tool `REQUIRED_OIDS` index from envelope `"tools"` map | Per-`(vendor, model)` alone can't prove "every tool has OIDs". | **Per-tool index**, validated at boot. |
| 4 | Catalog resolve at driver boundary, not tool boundary | One chokepoint for literal warning + per-tool index. | **Driver boundary.** Tool → driver → `resolve(...)` → wire. |
| 5 | Pre-resolve TWO triples for `snmp_migrate_radio_frequency` | Source + destination firmware both need signed catalogs before SET. | **`resolve_migration_refs(...)`** — raises before wire. |
| 6 | Protocol admits SSH/REST; only SNMP ships | Vendor detail must never cross into `server.py` / `intervention_*`. | **Protocol-only seam.** R-NEW-4 AST extended. |
| 7 | `threading.Timer` watchdog | MCP tool layer owns no event loop; sync is the seam. | **Timer** default 300s; loss → revert + POST_MIGRATION. |

## Module Layout

```
NEW  drivers/interface.py            DeviceDriverInterface (Protocol, @runtime_checkable)
NEW  drivers/resolver.py             DeviceResolver.build(host, v, creds) -> Device
NEW  hitl/tokens.py                  verify_approval_token (slice 4)
NEW  drivers/snmp_pmp450i/{summaries,subscribers,spectrum,migrate}.py  (slices 2-4)
MOD  drivers/snmp_pmp450i/driver.py  Pmp450iSnmpDriver + report_firmware()
MOD  drivers/exceptions.py           +3 typed exceptions
MOD  drivers/oid_catalog.py          envelope.tools + REQUIRED_OIDS_BY_TOOL
MOD  config.py                       nora_hitl_rollback_timeout_seconds=300
                                    nora_hitl_token_ttl_seconds=900
MOD  cli.py                          registration guard (slice 5)
MOD  server.py                       5 → 11 @mcp.tool wrappers + HITL text
MOD  data/oid-catalogs/.../15.2.1.json  re-signed each slice
```

## Sequence: IP-Direct Read (Slice 1)

```
client ─▶ @mcp.tool ─▶ Pmp450iSnmpDriver ─▶ OidCatalogRegistry
                          │                         │
                          │ report_firmware()       │ resolve((cambium, pmp450i, 15.3.0))
                          │ ─▶ Version("15.3.0")    │ ─▶ OidCatalog + stderr warn
                          ▼                         ▼
                        SnmpClient.get_oid(...) ─▶ typed scalars
                          ▼
                        fold → ApSummary → Sanitizer.sanitize(free-text) → JSON
```

## Sequence: HITL Approval + Migration + Watchdog (Slice 4)

```
client ─▶ snmp_migrate_radio_frequency(approval_token, target_frequency_mhz)
          │
          ▼
      hitl.tokens.verify_approval_token(token)
          │
   ┌──────┴──────┐
missing/invalid  valid
   │              │
   ▼              ▼
AutonomousMut.   OidCatalogRegistry.resolve_migration_refs(current, candidate)
Rejected         │ (raises before any SET)
(no SET)         ▼
                Pmp450iSnmpDriver:
                  categorize → migrate ONLINE_ACTIVE → SET AP carrier last
                Timer(300s) armed ─────────────┐
                     │                         │ polls snmp_get_ap_summary
              ┌──────┴──────┐                  │
      reach mgmt <300s   loss-of-mgmt         │
              │           (timeout)           │
              ▼                ▼              │
          cancel         revert SET ◀─────────┘
              │           (prior carrier)
              └──────┬─────┘
                     ▼
      save_intervention_record(stage="POST_MIGRATION", rolled_back, reason?)
                     ▼
                 {rolled_back: bool, reason?: str}
```

## Slice-to-Architecture Mapping

| # | NEW / MODIFIED | Tests |
|---|----------------|-------|
| 1 | `drivers/{interface,resolver}.py`, `snmp_pmp450i/driver.py`, `registry.py`, `exceptions.py`; 2 tests | 4 |
| 2 | `snmp_pmp450i/summaries.py`, `server.py`, `oid_catalog.py`, catalog re-sign; 1 test | 4 |
| 3 | `snmp_pmp450i/subscribers.py`, `server.py`, catalog re-sign; 1 test | 6 |
| 4 | `snmp_pmp450i/{spectrum,migrate}.py`, `hitl/tokens.py`, `server.py`, `config.py`, `exceptions.py`, catalog re-sign; 3 tests | 8 |
| 5 | `cli.py` (guard), `oid_catalog.py` (per-tool index); 1 test | 5 |

Dep chain: 1 → 2 → 3 → 4 → 5.

## Vendor Isolation + Zero-Leakage

`DeviceDriverInterface` admits SSH/REST; only `Pmp450iSnmpDriver` ships. Cambium detail stays in `drivers/snmp_pmp450i/` and `data/oid-catalogs/cambium/`. R-NEW-4 hardened via `tests/test_no_llm_journal_imports.py` (one-way-dep scan) + `tests/test_driver_airgap.py` (`drivers/resolver.py` covered). `DeviceResolver` reads creds from `Settings` only; every credential is `SecretStr`. Every tool response free-text field passes `Sanitizer.sanitize(...)`; typed scalars bypass. No IP/hostname/serial/credential literals in any artifact.

## Failure Modes + Mitigations

| Failure | Sev | Mitigation |
|---------|-----|------------|
| HITL bypass via direct MCP call | Critical | Stub always raises; pair-review slice 4 |
| `DeviceResolver` leaks credentials | High | All creds `SecretStr`; repr-safety test; AST extended |
| `PRE_EXISTING_OFFLINE` boundary drifts | High | ONE `categorize_subscribers(...)`; cross-check test |
| Loss-of-management mid-migration | High | `threading.Timer` watchdog 300s; revert SET |
| Catalog minor-mismatch silent on tool paths | Med | Tool → driver → `resolve(...)`; slice 5 E2E |
| Uncatalogued tool exposed | Med | Registration-time guard raises `UncataloguedToolError`; aborts `mcp.run()` |

## Threat Matrix

`N/A` — no routing, shell, subprocess, VCS/PR automation, or process-integration boundary introduced. Drivers use existing `SnmpClient`; slice 4 watchdog is `threading.Timer` (in-process).

## Migration / Rollout

No data migration. Each PR revert removes the slice's `@mcp.tool`s + module; `devices.yaml` and HMAC-pinned catalogs remain valid. Slice 4 backout: `nora_hitl_token_ttl_seconds=0` makes the stub reject every token. Slice 5 backout: drop the guard + test; #24 stays open.

## Out of Scope

Real SSH/REST drivers (Protocol admits; SNMP ships). Second vendor beyond Cambium (ADR #17 P3). Full HITL `ChangeRequest` state machine (Phase-3 cluster). Key rotation (operational).
