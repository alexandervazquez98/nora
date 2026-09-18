# Feature: Multi-community migration, band-aware frequency migration, post-band-change reboot

**Branch**: `feat/multi-community-band-reboot` (NOT created yet — this document is the scope brief; the branch lands with WU-A)
**Source**: Operator-reported field incidents on BAJ01 + spectrum band-crossing attempts (Sept 2026)
**Mode**: Three sequenced work units; pre-flight validation in WU-A, catalog layer in WU-B, new Tier-2 tool in WU-C
**Delivery**: Single feature branch, three work-unit commits stacked (no merge until user decides)

---

## MIB analysis findings (added 2026-09-18 after operator shared local MIB bundle)

The operator shared two Cambium WHISP MIB bundles from `~/Downloads/`:

* `PM450 25.0.1 MIBS.zip` — Cambium PMP 450 firmware 25.0.1
* `PMP450 Series 25.1 MIBS.zip` — Cambium PMP 450 firmware 25.1

Both bundles extract to the same 7 files: `IANAifType-MIB.txt`, `IF-MIB.txt`, `WHISP-APS-MIB.txt`, `WHISP-BOX-MIBV2-MIB.txt`, `WHISP-GLOBAL-REG-MIB.txt`, `WHISP-SM-MIB.txt`, `WHISP-TCV2-MIB.txt`.

> **Discrepancy flag** — the operator originally said the missing firmwares were **20.0.1 and 20.1**. The local bundles are 25.0.1 and 25.1, not 20.x. Two possibilities: (a) the operator's firmware-naming memory was approximate and the field SMs are actually on 25.0.1 / 25.1, or (b) the operator has separate 20.x MIBs somewhere else (not in `~/Downloads/`). Before WU-B starts we need the operator to confirm the actual firmware version on the field SMs.

Against the 25.x MIBs, the catalog work revealed **a critical pre-existing bug** in the shipped catalogs that goes beyond WU-B's scope:

### Finding 1 — `frequency` / `migrate*` OIDs in all three shipped catalogs point at a deprecated symbol

The signed catalogs `data/oid-catalogs/cambium/pmp450i/15.2.1.json`, `15.3.0.json`, and `25.1.0.json` all map `frequency`, `migrateCarrierFrequency`, and `migratePriorCarrierFrequency` to:

```
1.3.6.1.4.1.161.19.3.1.1.2.0
```

That dotted OID resolves (via `whispApsConfig 2` in the 25.x MIB) to:

```
rfFreqCarrier OBJECT-TYPE
    SYNTAX INTEGER { wired(0) }
    UNITS "kHz"
    MAX-ACCESS read-write
    STATUS deprecated
    DESCRIPTION
        "The primary transmit frequency. Also see radioFreqCarrier."
```

`STATUS deprecated` and `SYNTAX INTEGER { wired(0) }` means the firmware only accepts `wired(0)`. **Any SET with a real frequency value is rejected by the agent with `badValue` / `wrongValue` — the exact failure mode the operator sees as "no está en la lista".** The same OID is read for `frequency` (the agent returns `0` in normal operation, which the driver silently coerces).

The correct symbol, per the 25.x MIB:

```
radioFreqCarrier OBJECT-TYPE
    SYNTAX INTEGER (any non-zero integer accepted as kHz)
    UNITS "kHz"
    MAX-ACCESS read-write
    STATUS current
    DESCRIPTION
        "RF Frequency. Please see the whispBoxRFPhysicalRadioFrequencies
        SNMP table for a list of available frequencies.
        0: wired.
        As of release 16.1, this OID no longer requires reboot to take
        affect. It will be applied immediately. SMs will be signaled to
        the configured frequency."
    ::= {whispApsRFConfigRadioEntry 1}
```

This is the exact failure mode the operator reported as Problem 2. **WU-B cannot land cleanly without first fixing the three shipped catalogs** — they need a new `radioFreqCarrier` OID entry (with index `1.<radioIndex>.1`) replacing the deprecated `rfFreqCarrier` alias. The new catalog must be re-signed; the HMAC change forces a coordinated cut.

### Finding 2 — Reboot is NOT required for frequency changes (>= 16.1 firmware), but IS required for band-class changes

The 25.x MIB DESCRIPTION for `radioFreqCarrier` says verbatim: *"As of release 16.1, this OID no longer requires reboot to take affect."* This contradicts the operator's mental model that 4.9↔5.x requires a reboot.

The reboot itself is still a real, supported operation:

```
reboot OBJECT-TYPE
    SYNTAX INTEGER { finishedReboot(0), reboot(1), fullReboot(2) }
    MAX-ACCESS read-write
    STATUS current
    ::={whispBoxControls 2}
```

(resolves to `1.3.6.1.4.1.161.19.3.3.3.2.0`). It exists for two cases: (a) clearing an `apply` failure, (b) flipping the band class via `radioFrequencyBand` (which carries the explicit warning "Set is Engineering use only" — band flips are not exposed through the Web GUI either). The 25.x MIB also has `rebootIfRequired` (`whispBoxControls 4`) that returns 1 if a reboot is pending for any reason — a much better signal than our planned `band_crossing` detector.

### Finding 3 — The "list" the operator sees is `whispBoxRFPhysicalRadioFrequencies`

```
whispBoxRFPhysicalRadioFrequencies OBJECT-TYPE
    SYNTAX SEQUENCE OF WhispBoxRFPhysicalRadioFrequencyEntry
    DESCRIPTION "Available frequency information table."
    ::= {whispBoxRFPhysical 3}

WhispBoxRFPhysicalRadioFrequencyEntry ::= SEQUENCE { frequency INTEGER }
INDEX { radioIndex, frequency }
frequency OBJECT-TYPE
    SYNTAX INTEGER (0..9000000)
    MAX-ACCESS read-only
    STATUS current
    DESCRIPTION "Frequency."
    ::= {whispBoxRFPhysicalRadioFrequencyEntry 1}
```

A read-only table walk of this subtree (rooted at `1.3.6.1.4.1.161.19.3.3.15.3`) returns exactly the values the agent will accept on `radioFreqCarrier` SET. **NORA should walk this table BEFORE issuing any `migrateCarrierFrequency` SET and reject the migration at the pre-flight stage if `target_frequency_mhz` is not in the walk result** — this is the missing "allowed frequencies" guard the operator was asking for in Problem 2.

### Finding 4 — `radioFrequencyBand` enum covers all PMP 450 bands including 4.9 GHz

```
radioFrequencyBand OBJECT-TYPE
    SYNTAX INTEGER {
        band700(0), band900(1), band2400(2), band3500(3), band3700(4),
        band4900(5), band5100(6), band5200(7), band5400(8), band5700(9),
        band5800(10), band5900(11), band6050(12), band3600(13), band4959(14),
        band3(15), band5170(16), band65(17), band67(18), band5763(19), band66(20)
    }
    MAX-ACCESS read-write
    STATUS current
    DESCRIPTION "Currently configured radio band. Set is Engineering use only."
    ::= {whispBoxRFConfigRadioEntry 2}
```

Resolves to `1.3.6.1.4.1.161.19.3.3.16.1.<idx>.2`. NORA should read this BEFORE the migration; a SET on it requires the reboot tool from Finding 2.

### Finding 5 — Other notable OIDs the catalog ships but the MIB confirms

* `spectrumAnalysisActionBox` (`whispBoxConfig 221` → `1.3.6.1.4.1.161.19.3.3.2.221.0`) — confirms the existing `spectrumScanStatus` OID in the catalog is correct. The MIB also documents the action semantics (start/stop/continuous/idle/in-progress/notReady).
* `activeTxPowerStr` (`whispBoxStatus 232` → `1.3.6.1.4.1.161.19.3.3.1.232.0`) — confirms the existing `transmitPower` OID in the catalog is correct (read-only DisplayString).
* `channelBandwidth` (`whispBoxConfig 83` → `1.3.6.1.4.1.161.19.3.3.2.83.0`) — confirms the existing `channelBandwidth` OID in the catalog is correct.

---

## Background — the three verified incidents

Three concrete field reports, all reproducible against current code + signed catalogs:

### Incident 1 — BAJ01 with heterogeneous SNMP communities aborts the migration

The PMP 450i AP at BAJ01 has SMs registered against it whose SNMP community strings differ from the AP's own community (e.g. AP community `BAJ01-AP-COMM`, SMs `BAJ01-SM-A-COMM`, `BAJ01-SM-B-COMM`). The current migration flow (`src/nora/drivers/snmp_pmp450i/migrate.py:288`) builds **one** `SnmpClient` from `driver._client_factory(device)` where `device` is the AP, then **reuses that same client** to walk every SM row from the `whispApsLinkTable` and (in the placeholder body) to reach each SM. The SM table walk itself succeeds (it is read on the AP, so the AP community authenticates), but any per-SM action that requires authenticating as the SM — including the real SET that the placeholder `migrate_subscriber` (`migrate.py:140-156`) currently skips — fails with `puresnmp.exc.SnmpError` (auth rejected).

The downstream effect on the orchestrator: `online_active_migrated=0, active_degraded_migrated=0`, `record_status="ABORTED"`, intervention log records `ABORTED`. From the openchat UI side it looks like the system "batalla" to consider those other communities and gives up.

### Incident 2 — Cross-band frequency migration rejected as "not in the list"

`spectrum.py:42` pins the candidate set to `_MHZ_BASE_FREQS = (5780, 5800, 5820)` — a single 5.78–5.82 GHz sub-band. `snmp_migrate_radio_frequency` accepts `target_frequency_mhz: float` and emits a raw SET on `migrateCarrierFrequency` (catalog OID `1.3.6.1.4.1.161.19.3.1.1.2.0`) without consulting the radio's `allowedFrequencyList` / `bandPlan`. Cambium PMP 450i segments 5 GHz into distinct regulatory bands (5.1, 5.2, 5.4, 5.7 GHz) and 4.9 GHz Public Safety as a separate band class. A SET that targets a frequency outside the radio's currently configured band is rejected by the firmware with a `badValue` / `wrongValue` SNMP error, surfaced as "the frequency is not in the list" both on the AP side and on the SM side (SMs fail to resync to the new carrier).

### Incident 3 — Band crossing requires reboot (4.9 ↔ 5.x)

Cambium PMP 450i requires a reboot when the configured band changes (5.x → 4.9 or 4.9 → 5.x). After a successful `migrateCarrierFrequency` SET, the AP and SMs stay on the old band until a reboot cycle flips the band class. The migration result reports `rolled_back=false, online_active_migrated=N` but the SMs never actually retune. There is no reboot tool in NORA today — the orchestrator prompt has a single line (`netops_orchestrator.md:63`) that says "reboots autonomously" is forbidden, but no operator-facing mechanism to do it under HITL.

---

## Scope

Three sequenced WUs, each one work-unit commit. The feature is "complete" when all three commits land on `feat/multi-community-band-reboot`. No PR open yet — the branch is created when WU-A work starts.

### Non-goals

- No write mutations on the read-only `SnmpClient` Protocol (`client.py:24-39`). Write verbs land only in `migrate.py` and the new `reboot.py` through the existing `apply_oid` seam (kept as a dry-run fallback in production per WU-3 of `pr44-followups.md`).
- No catalog re-sign of the existing baselines (`15.2.1.json`, `15.3.0.json`, `25.1.0.json`) — WU-B only **adds** `20.0.1.json` and `20.1.json`, never modifies.
- No bumping of the package version; release decision is the user's.
- No change to the openchat UI shim surface (`shim_webui.py`) in this feature. The shim exposes the 3 read-only intervention-memory tools today; the new behavior happens at the `nora-mcp` server side and the openchat orchestrator consumes it via MCP directly.

---

## Confirmed decisions (today, 2026-09-18)

1. **Pre-flight validation runs BEFORE the HITL gate** in `snmp_migrate_radio_frequency`. A failed pre-flight aborts with a typed message naming the SM IP / LUID and the rejected community; the operator is asked to confirm or supply a different community. No HITL token is minted in the failed-pre-flight path.
2. **Reboot is a new Tier-2 tool**: `snmp_reboot_radio(device_id, approval_token)`. The orchestrator invokes it explicitly after `snmp_migrate_radio_frequency` only when the migration crossed a band boundary. Two HITL tokens in that flow (one for the migration, one for the reboot) — the cost is justified because reboot is independently disruptive.
3. **MIB / catalog layer**: WU-B downloads the Cambium WHISP MIB bundle for firmware 20.x (and the matching SM-side MIBs), walks an AP / SM if reachable in the lab to capture the live OID tree, and signs `data/oid-catalogs/cambium/pmp450i/20.0.1.json` and `20.1.json` with the same `scripts/sign_catalog.py` flow as the existing baselines.

---

## Work units

### WU-A — Pre-flight community validation in `snmp_migrate_radio_frequency`

**Symptom fixed**: heterogeneous SNMP communities across AP + SMs at the same site (BAJ01 and similar) cause the migration to silently produce `online_active_migrated=0, active_degraded_migrated=0` and abort. The operator sees no actionable error.

**Fix**:
- Introduce a `validate_communities(driver, device_id, settings)` helper in `src/nora/drivers/snmp_pmp450i/migrate.py` (or a new sibling module `preflight.py` if the file grows past ~500 LOC). The helper:
  1. Resolves the inventory entries for the AP + every SM listed in the cross-checked SM table (using `snmp_get_sm_table` data — pulls ONLINE_ACTIVE + ACTIVE_DEGRADED rows, NOT PRE_EXISTING_OFFLINE).
  2. For each candidate SM that is in `Inventory`, opens a fresh `SnmpClient` with **that SM's own `community`** (v2c) or its v3 credentials, and issues a single `sysDescr` GET against OID `1.3.6.1.2.1.1.1.0` — same pattern as `_validate_sysdescr` in `register_device.py:159-176`.
  3. For SMs missing from `Inventory` entirely (the SM-table returned LUIDs that don't resolve to a `Device` in the inventory): emit a `MISSING_INVENTORY_ENTRY` advisory in the result — the operator must register the SM first via `register_device`.
  4. Translates wire failures to typed exceptions already in the catalog: `DeviceUnreachable` for `OSError` / `TimeoutError`, `InvalidCommunity` for `puresnmp.exc.SnmpError`.
  5. Returns a typed `PreFlightReport` (Pydantic, frozen) carrying `ap_reachable: bool`, `ap_sysdescr: str | None`, `sm_results: list[SmPreFlightResult]` (one per SM, with `luid`, `host`, `reachable: bool`, `community_accepted: bool`, `error_class: str | None`, `error_message: str | None`), `missing_inventory_luids: list[str]`.
- The pre-flight runs AFTER the inventory resolution and BEFORE the HITL token verification in `fetch_migrate`. On any `DeviceUnreachable` or `InvalidCommunity` for any SM, the helper raises a typed `CommunityValidationFailed(PreFlightReport)` carrying the full report so the MCP tool boundary returns the structured report to the orchestrator.
- The orchestrator prompt (`netops_orchestrator.md`) gains a new step in §5 HITL section: "On `CommunityValidationFailed`, surface the failed SMs to the operator with ip/LUID and the community mismatch message; pause and ask the operator to either (a) confirm the community in `devices.yaml`, or (b) supply a different community string for that SM via `register_device`."
- The rollback semantics from the existing `MigrationResult` (`rolled_back`, `reason`) stay unchanged when pre-flight passes.

**Files**:
- `src/nora/drivers/snmp_pmp450i/migrate.py` — add `PreFlightReport`, `SmPreFlightResult`, `CommunityValidationFailed`, `validate_communities`; wire into `fetch_migrate` BEFORE `verify_approval_token`.
- `src/nora/drivers/exceptions.py` — add `CommunityValidationFailed` typed exception.
- `src/nora/prompts/netops_orchestrator.md` — extend §5 with the `CommunityValidationFailed` operator-facing message contract.
- `tests/test_snmp_migrate.py` — RED-first: 5 scenarios (AP reachable + all SMs reachable → pass; one SM unreachable → `DeviceUnreachable` in report; one SM wrong community → `InvalidCommunity`; SM not in inventory → `MISSING_INVENTORY_ENTRY`; mix of failures).
- `tests/test_exceptions.py` — add typed-exception coverage.

**Evidence gate**: `pytest tests/test_snmp_migrate.py tests/test_exceptions.py -v`; integration test against a simulated heterogeneous-community sector with at least one SM with the wrong community.

**Tier impact**: none — pre-flight is a precondition to the existing Tier-2 `snmp_migrate_radio_frequency`. It runs **before** the HITL gate; no token spent on a known-failed migration.

### WU-B — Add signed OID catalogs for Cambium PMP 450i firmware 20.0.1 and 20.1 + fix shipped-catalog `radioFreqCarrier` bug

**Symptom fixed**: two distinct gaps — (a) most field SMs run firmware 20.x but the current signed catalog set (`15.2.1.json`, `15.3.0.json`, `25.1.0.json`) does not cover 20.x; (b) all three shipped catalogs map `frequency` / `migrateCarrierFrequency` / `migratePriorCarrierFrequency` to the deprecated `rfFreqCarrier` (`1.3.6.1.4.1.161.19.3.1.1.2.0`), which only accepts `wired(0)` and rejects any real frequency value with `badValue` / `wrongValue`. (b) is the root cause of the operator's Problem 2 ("no está en la lista"); the existing catalog bug silently blocks every migration. Fixing (b) is a prerequisite for WU-A and WU-C to function correctly, so WU-B leads.

**Fix** (in execution order):
1. **Confirm the actual field firmware** — the operator said 20.x but the local MIB bundles are 25.0.1 and 25.1. Walk or `apFirmwareVersion` GET against two or three field SMs to pin the actual firmware string before signing any catalog. If the SMs really are 20.x, request the 20.x MIB bundle separately; if they are 25.x the new catalogs become 25.0.1 and 25.1 envelopes instead.
2. **Re-sign the three shipped catalogs** (`15.2.1.json`, `15.3.0.json`, `25.1.0.json`) replacing the deprecated `rfFreqCarrier` OID with the current `radioFreqCarrier` (root `whispApsRFConfigRadioEntry 1` → `1.3.6.1.4.1.161.19.3.1.10.<radioIndex>.1`). Add the new aliases `radioFreqCarrier` (SET) and keep `frequency` (the GET alias documented in the MIB). **This invalidates the HMAC on each shipped envelope and is a coordinated cut** — operators must roll the new catalogs and the new server boot simultaneously. Document the cut in `docs/oid-catalog-baselines.md` with the version-bump rule (catalog `version` field increments from 1 to 2).
3. **Add `rebootIfRequired` OID** (`1.3.6.1.4.1.161.19.3.3.3.4.0`, `whispBoxControls 4`) and `reboot` OID (`1.3.6.1.4.1.161.19.3.3.3.2.0`, `whispBoxControls 2`) to every shipped and new catalog. WU-C reads `rebootIfRequired` first; only sets `reboot` when the radio says one is required.
4. **Add `radioFrequencyBand` OID** (`1.3.6.1.4.1.161.19.3.3.16.1.<radioIndex>.2`, `whispBoxRFConfigRadioEntry 2`) to every catalog. The band-class guard in WU-C reads this to detect band crossing (5.x ↔ 4.9 vs same-band sub-channel change).
5. **Add `whispBoxRFPhysicalRadioFrequencies` table OID** (`1.3.6.1.4.1.161.19.3.3.15.3`, `whispBoxRFPhysical 3`) to every catalog — the WU-A pre-flight walks this subtree to produce the per-band "allowed frequencies" list and reject `target_frequency_mhz` if absent.
6. **Build the new 20.x (or 25.x) catalogs** with the same envelope shape. Sign with `scripts/sign_catalog.py` using `NORA_OID_CATALOGS_SIGNING_KEY`.
7. **Verify strict-major hard fail** still works: a `device` reporting firmware `20.0.1` resolves to the new `20.0.1.json`, never to `15.x` or `25.x` baselines. Same for `25.1` vs `25.1.0`.

**Files**:
- `data/oid-catalogs/cambium/pmp450i/15.2.1.json`, `15.3.0.json`, `25.1.0.json` — re-signed envelopes (catalog `version: 2`).
- `data/oid-catalogs/cambium/pmp450i/20.0.1.json`, `20.1.json` (or `25.0.1.json`, `25.1.json` if firmware confirmation says so) — new signed envelopes.
- `data/oid-catalogs/cambium/pmp450i/CHANGELOG.md` — new file documenting the OID corrections per firmware baseline.
- `scripts/sign_catalog.py` — verify it accepts the new envelope fields without changes (likely no edit; if a path needs editing, scope as a sub-WU).
- `tests/test_oid_catalog_integration.py` — RED-first: scenarios for catalog `version: 2` cut, the new OID names (`radioFreqCarrier`, `reboot`, `rebootIfRequired`, `radioFrequencyBand`, `whispBoxRFPhysicalRadioFrequencies`), strict-major refusal of `20 → 15/25`, HMAC verification for every new envelope.
- `docs/oid-catalog-baselines.md` — document the version-bump rule, the deprecated-OID removals, and the operator-facing cut plan.

**Evidence gate**: `pytest tests/test_oid_catalog_integration.py -v`; the catalog verifier accepts every re-signed and new envelope at boot; `OidCatalogRegistry.resolve(("cambium", "pmp450i", "<firmware>"))` returns the right envelope with `radioFreqCarrier` present; a unit test asserts `radioFreqCarrier` is the SET target (not the deprecated `rfFreqCarrier`) for the `snmp_migrate_radio_frequency` tool.

**Tier impact**: none — catalog additions and corrections are passive, but the cut is operationally disruptive (the migration tool stops working in environments that have not picked up the new catalogs).

### WU-C — New Tier-2 tool `snmp_reboot_radio` with band-crossing detection

**Symptom fixed**: after a successful frequency migration that crosses regulatory bands (5.x → 4.9 or 4.9 → 5.x), the AP and SMs require a reboot for the band class to flip. With the shipped-catalog `radioFreqCarrier` correction from WU-B, the SET itself succeeds for firmware ≥ 16.1, but band-class flips still require a reboot (per the 25.x MIB `radioFrequencyBand` DESCRIPTION: "Set is Engineering use only" — the firmware expects the device to restart). The current migration reports success but the SMs do not actually retune on band-class crossings. Operators must manually reboot from the GUI.

**Fix**:
- Add `data/oid-catalogs/cambium/pmp450i/<baseline>.json` entries for the reboot OID once WU-B confirms the dotted path. (Cambium PMP 450i reboot is documented in the WHISP-BOX-MIBV2-MIB as a SET on a private OID; placeholder until WU-B resolves.)
- New module `src/nora/drivers/snmp_pmp450i/reboot.py` carrying a `RebootResult` Pydantic model (frozen): `device_id`, `rebooted: bool`, `dry_run: bool`, `would_set: list[tuple[str, str | int | float]]`, `expected_recovery_seconds: int`, `hitl_required: bool = True`.
- New `fetch_reboot(*, driver, device_id, approval_token, settings)` helper:
  1. Verifies `verify_approval_token` FIRST (Tier-2 gate, same pattern as `fetch_migrate`).
  2. Resolves device + catalog; confirms the reboot OID is present (raises `LookupError` otherwise — boot-time catalog guard should also reject).
  3. Detects `apply_oid` on the client (dry-run fallback per WU-3 of `pr44-followups.md`): when present, emits the SET; when absent, returns `RebootResult(dry_run=True, would_set=[(reboot_oid, 1)])`.
  4. Emits one `POST_REBOOT` intervention record per completion (mirror of the `POST_MIGRATION` record shape, with `stage="POST_REBOOT"`, `record_name="RF reboot of {device_id}"`, `status=COMPLETED|DRY_RUN`).
- New `@mcp.tool snmp_reboot_radio(device_id, approval_token)` in `server.py`, registered alongside `snmp_migrate_radio_frequency`. Tier-2 classification: `tier: 2`, `requires_hitl_token: true`. New tool-spec at `docs/tool_specs/snmp_reboot_radio.md` matching the frozen schema.
- Extend `_EXPECTED_TOOL_TIERS` in `server.py` to include `"snmp_reboot_radio": 2` (currently only includes `snmp_migrate_radio_frequency: 2` and `save_intervention_record: 2`).
- New band-crossing detector in `migrate.py` (or a sibling `band_plan.py`):
  1. Read the AP's current `frequency` OID and `target_frequency_mhz` from the migration request.
  2. Resolve the regulatory band of each via a small table mapping (5.1 / 5.2 / 5.4 / 5.7 / 4.9 GHz). The table is hard-coded for v1; future WU pulls it from the catalog if WU-B finds an OID for it.
  3. **Band-crossing detection** uses two signals in order: first read `radioFrequencyBand` (`whispBoxRFConfigRadioEntry 2`) for the current band class; then read `rebootIfRequired` (`whispBoxControls 4`). If `rebootIfRequired` returns `rebootRequired(1)` after a successful `radioFreqCarrier` SET, the migration result carries `band_crossing: true` (or — more accurately — `reboot_required: true`). Per the 25.x MIB DESCRIPTION for `radioFreqCarrier`: frequency SETs on firmware ≥ 16.1 do NOT require a reboot, so the band-crossing flag fires only on a real band-class change (5.x → 4.9 or vice versa), not on a same-band sub-channel change. Use `radioFrequencyBand` delta to decide whether a reboot is operationally required; use `rebootIfRequired` as the firmware's authoritative vote.
- Extend the orchestrator prompt `netops_orchestrator.md` §5 with: "When `MigrationResult.band_crossing == True`, pause and request a second HITL token via `nora hitl mint` for the reboot; then call `snmp_reboot_radio(device_id, approval_token)`. Confirm the operator acknowledged the band-crossing impact before minting."

**Files**:
- `src/nora/drivers/snmp_pmp450i/reboot.py` — new module: `RebootResult`, `fetch_reboot`.
- `src/nora/drivers/snmp_pmp450i/migrate.py` — add `band_crossing: bool` to `MigrationResult`; new `BandPlan.detect_band_crossing(current_mhz, target_mhz)` helper.
- `src/nora/server.py` — new `@mcp.tool snmp_reboot_radio`; extend `_EXPECTED_TOOL_TIERS` and `_ALLOWED_UNCATALOGUED_TOOLS` if the reboot OID is not yet in the WU-B catalog.
- `src/nora/data/oid-catalogs/cambium/pmp450i/15.2.1.json`, `15.3.0.json`, `25.1.0.json`, `20.0.1.json`, `20.1.json` — add `rebootRadio` OID name where applicable (one name per baseline that resolves to the right dotted OID for that firmware).
- `src/nora/prompts/netops_orchestrator.md` — extend §5 with the band-crossing → reboot flow.
- `docs/tool_specs/snmp_reboot_radio.md` — new tool-spec.
- `tests/test_snmp_reboot.py` — RED-first: 4 scenarios (happy path real SET, dry-run fallback, missing approval token → `AutonomousMutationRejected`, missing reboot OID → `LookupError`).
- `tests/test_band_plan.py` — RED-first: 6 scenarios (same-band 5.7 → 5.8 no crossing; cross-band 5.4 → 5.7 crossing; 5.x → 4.9 crossing; 4.9 → 5.x crossing; 4.9 → 4.9 same; band-table edge cases).
- `tests/test_snmp_migrate.py` — extend existing migrate tests with `band_crossing` field assertions.

**Evidence gate**: `pytest tests/test_snmp_reboot.py tests/test_band_plan.py tests/test_snmp_migrate.py tests/test_oid_catalog_integration.py -v`; boot-time tool-registration guard accepts `snmp_reboot_radio` (catalog entry present); dry-run fallback returns `RebootResult(dry_run=True)` against the production `V2CClient`.

**Tier impact**: introduces a new Tier-2 tool. Update `_EXPECTED_TOOL_TIERS` and `docs/tool_specs/` per the freeze pattern from `2026-09-15-3tier-tool-governance`.

---

## Sequencing & commits

| Order | WU | Commit title (Conventional Commits) | Estimated diff |
|----:|-----|------------------------------------|----------------|
| 1 | WU-A | `feat(migrate): pre-flight community validation against sysDescr per SM` | +150 LOC + tests |
| 2 | WU-B | `feat(oid-catalogs): add signed catalogs for cambium pmp450i 20.0.1 and 20.1` | +2 new JSON envelopes + ~40 LOC test |
| 3 | WU-C | `feat(pmp450i/reboot): snmp_reboot_radio Tier-2 tool + band-crossing detector` | +250 LOC + tests + tool-spec |

Total estimated diff: ~440 LOC + 2 catalog envelopes. Each WU is independently revertible. WU-A does not depend on WU-B; WU-C depends on WU-B for the reboot OID resolution.

---

## Risks & live-system rules

- All 3 commits ship on `feat/multi-community-band-reboot`. The branch is created when WU-A work starts; no commits will be made without going through work-unit-commit discipline (one WU = one commit, message above, footers reference the WU-id and feature doc).
- No commits before the operator confirms access to the Cambium support portal for WU-B's MIB bundle (or confirms fallback to MIB-derived catalog entries with the `derived_from_mib_only` provenance flag).
- No merge to main, no tag, no release. The user owns merge.
- After the 3 commits land, re-run the full pytest suite + ruff + mypy + coverage to confirm no regression. Then update the PR description to link each WU back to this feature document.
- RDD (Receipt-Driven Development): follow the gentle-pi review pipeline when the user enables it. By default we don't trigger reviews for bounded fixes; the user may opt in for one or more WUs.
- The pre-flight in WU-A is read-only against the radio (a single `sysDescr` GET per SM); it adds no wire-frame cost beyond what `register_device(validate=True)` already does, and the worst case is ~one extra second per SM at the standard 5-second puresnmp timeout.
- The new `snmp_reboot_radio` tool in WU-C is **independently HITL-gated**. The orchestrator must never chain it silently after `snmp_migrate_radio_frequency`; a second `nora hitl mint` is required, and the operator must explicitly acknowledge the band-crossing impact.

---

## Open follow-ups (post-feature, NOT in this scope)

- Pull the regulatory band table from the catalog instead of a hard-coded mapping (depends on a band-plan OID being documented by Cambium in a future MIB revision).
- Persist `band_crossing: bool` in the `POST_MIGRATION` intervention record schema (`intervention_memory/models.py`) so the read-side correlation tooling can flag historical band crossings.
- Extend `correlate_sector_interference` with an "is the new carrier inside the radio's allowed list" cross-check (becomes possible once WU-B's catalog lands).
- Generalize the pre-flight pattern beyond community validation: the same `_validate_sysdescr` seam can become a generic `preflight_probe(luids, oids)` helper for any read-only reachability / sanity check the orchestrator wants before a Tier-2 call.

---

## Status

- WU-A: scope drafted; no code yet. Blocked on user authorization to start.
- WU-B: scope drafted; MIB download pending user-supplied Cambium support URL.
- WU-C: scope drafted; depends on WU-B for the reboot OID resolution.

The operator owns the go-ahead for each WU.