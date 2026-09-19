# OID Catalog CHANGELOG

Per-envelope diff between shipped catalog versions. Each row is a contract for one `(vendor, model, firmware)` triple.

## v2 — 2026-09-18 — feat/multi-community-band-reboot — ODD cut

**Why**: Three operator-reported incidents on Cambium PMP 450i (BAJ01 multi-community abort, 5.x cross-band SET rejected as "not in the list", 4.9 GHz band-crossing requires reboot). Pre-existing bug in v1 catalogs masked `snmp_migrate_radio_frequency` as broken.

**What changed (every firmware baseline)**:

| OID name | v1 OID | v2 OID | Why |
|----------|--------|--------|-----|
| `frequency` | `1.3.6.1.4.1.161.19.3.1.1.2.0` | `1.3.6.1.4.1.161.19.3.1.10.1.1.1.1` | Re-pointed from deprecated `rfFreqCarrier` (SYNTAX `{ wired(0) }` only) to current `radioFreqCarrier` (leaf scalar: column 1, radioIndex 1) |
| `migrateCarrierFrequency` | `1.3.6.1.4.1.161.19.3.1.1.2.0` | `1.3.6.1.4.1.161.19.3.1.10.1.1.1.1` | Same correction |
| `migratePriorCarrierFrequency` | `1.3.6.1.4.1.161.19.3.1.1.2.0` | `1.3.6.1.4.1.161.19.3.1.10.1.1.1.1` | Same correction |

**What was added (every firmware baseline)**:

| OID name | OID | Source | Used by |
|----------|-----|--------|---------|
| `radioFreqCarrier` | `1.3.6.1.4.1.161.19.3.1.10.1.1.1.1` | WHISP-APS-MIB `whispApsRFConfigRadioEntry 1` (radioIndex=1) | Canonical SET target for `snmp_migrate_radio_frequency` |
| `reboot` | `1.3.6.1.4.1.161.19.3.3.3.2.0` | WHISP-BOX-MIBV2-MIB `whispBoxControls 2` | WU-C `snmp_reboot_radio` Tier-2 tool |
| `rebootIfRequired` | `1.3.6.1.4.1.161.19.3.3.3.4.0` | WHISP-BOX-MIBV2-MIB `whispBoxControls 4` | Preferred over band-crossing heuristics |
| `radioFrequencyBand` | `1.3.6.1.4.1.161.19.3.3.16.1.1.2` | WHISP-BOX-MIBV2-MIB `whispBoxRFConfigRadioEntry 2` (radioIndex=1) | Current band-class detection |
| `whispBoxRFPhysicalRadioFrequencies` | `1.3.6.1.4.1.161.19.3.3.15.3` | WHISP-BOX-MIBV2-MIB `whispBoxRFPhysical 3` | Root of the per-band allowed-frequencies table for WU-A pre-flight |

**What was NOT changed**: every other OID name in the v1 catalogs (radioDownlinkRate, signalStrength*, channelBandwidth, transmitPower, frameUtilization*, sm*, spectrum*, sysDescr) is unchanged. Confirmed by side-by-side diff of the `oids` map; cross-checked against the WHISP-BOX-MIBV2-MIB and WHISP-APS-MIB bundles shipped with the operator's `~/Downloads/PM450 25.0.1 MIBS.zip` and `~/Downloads/PMP450 Series 25.1 MIBS.zip`.

### Per-firmware envelopes touched

| Firmware | Status | Notes |
|----------|--------|-------|
| `15.2.1` | re-signed (v1 → v2) | OIDs updated; HMAC invalidated |
| `15.3.0` | re-signed (v1 → v2) | OIDs updated; HMAC invalidated |
| `25.1.0` | re-signed (v1 → v2) | OIDs updated; HMAC invalidated |
| `25.0.1` | **NEW** | Built from local MIB bundle; never shipped before |
| `25.1` | **NEW** | Built from local MIB bundle; firmware string is `25.1` (NOT `25.1.0`) to match what the operator's field SMs advertise via apFirmwareVersion |

## v1 — 2026-09-13 — feat(oid-catalog): real WHISP-APS-MIB positions + source-of-truth refactor

Shipped envelopes `15.2.1.json`, `15.3.0.json`, `25.1.0.json`. Pre-existing bug noted above (`frequency` / `migrate*` pointing at deprecated `rfFreqCarrier`).
