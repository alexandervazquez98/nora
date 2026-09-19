# OID Catalog Baselines & HMAC-SHA256 Rollout

**Owner**: NORA / Operators
**Last updated**: 2026-09-18
**Status**: Active cut — see `data/oid-catalogs/CHANGELOG.md` for the per-envelope diff.

## Overview

NORA verifies every OID catalog at boot via HMAC-SHA256 against a per-root signing key (`OidCatalogRegistry.verify_all`). Two roots:

| Root | Path | Signing key | Purpose |
|------|------|-------------|---------|
| **Built-in** | `src/nora/data/oid-catalogs/` (resolved at runtime via `importlib.resources.files("nora.data.oid-catalogs")`) | `BUILTIN_BASELINE_SIGNING_KEY` (constant in `src/nora/data/__init__.py`) | Ships in the wheel / source checkout. Placeholder key (`"nora-built-in-baseline-placeholder-key-do-not-use-in-prod"`) — NEVER use in production. |
| **Operator** | `Settings.nora_oid_catalogs_path` (default: `data/oid-catalogs/`) | `Settings.nora_oid_catalog_signing_key` (env: `NORA_OID_CATALOG_SIGNING_KEY`) | Operator-managed. Generated per-install by `scripts/generate_signing_key.py` (32 URL-safe-base64 bytes). Operator copy wins on a `(vendor, model, firmware)` collision. |

## Current envelopes

After the v2 cut (2026-09-18, feature `feat/multi-community-band-reboot`, ODD):

| Firmware | Built-in (`src/nora/data/oid-catalogs/`) | Operator (`data/oid-catalogs/`) | Version |
|----------|-----------------------------------------|--------------------------------|---------|
| 15.2.1 | yes | yes | 2 |
| 15.3.0 | yes | yes | 2 |
| 25.0.1 | yes (new) | yes (new) | 2 |
| 25.1.0 | yes | yes | 2 |
| 25.1   | yes (new) | yes (new) | 2 |

Strict-major hard fail: a device reporting firmware `20.0.1` does NOT resolve to any registered envelope (registered majors: 15, 25). Update `25.0.1` → `20.0.1` semantics in a follow-up PR if the field fleet actually runs 20.x firmware.

## What's new in v2

Per `data/oid-catalogs/CHANGELOG.md`:

1. `frequency` / `migrateCarrierFrequency` / `migratePriorCarrierFrequency` re-pointed from `1.3.6.1.4.1.161.19.3.1.1.2.0` (`rfFreqCarrier`, **STATUS deprecated**, `SYNTAX INTEGER { wired(0) }` only) to `1.3.6.1.4.1.161.19.3.1.10.1.1.1.1` (`radioFreqCarrier`, **STATUS current**, `MAX-ACCESS read-write`, leaf scalar instance: column 1, radioIndex 1). The old OID silently rejected every non-zero SET with `badValue` / `wrongValue`; the firmware surfaced this as "no está en la lista" on the operator's UI. This was a pre-existing bug in v1 envelopes that masked the snmp_migrate_radio_frequency tool as broken.
2. `radioFreqCarrier` added as an explicit OID name alongside `frequency` (alias). Both resolve to `1.3.6.1.4.1.161.19.3.1.10.1.1.1.1`; `radioFreqCarrier` is the canonical name from the WHISP-APS-MIB.
3. `reboot` (1.3.6.1.4.1.161.19.3.3.3.2.0) — for `snmp_reboot_radio` (WU-C).
4. `rebootIfRequired` (1.3.6.1.4.1.161.19.3.3.3.4.0) — preferred over heuristic band-crossing detection.
5. `radioFrequencyBand` (1.3.6.1.4.1.161.19.3.3.16.1.1.2) — enum covering 700 / 900 / 2400 / 3500 / 3700 / 4900 / 5100 / 5200 / 5400 / 5700 / 5800 / 5900 / 6050 / 3600 / 4959 / 3 / 5170 / 65 / 67 / 5763 / 66 MHz. Read current band for the band-crossing detector.
6. `whispBoxRFPhysicalRadioFrequencies` (1.3.6.1.4.1.161.19.3.3.15.3) — root of the read-only table the agent populates with the exact frequency values it accepts on `radioFreqCarrier` SET. The WU-A pre-flight walks this subtree to produce the per-band "allowed frequencies" list.

## Rollout plan (production)

This is a coordinated cut because **the shipped built-in envelopes are re-signed** and **the OID re-pointing changes wire behaviour**: an operator who has been running `snmp_migrate_radio_frequency` against v1 envelopes has been seeing silent failures on every frequency migration attempt. v2 envelopes fix the silent failure.

### For operators who already have NORA running

1. Pull `feat/multi-community-band-reboot` (or the merged equivalent).
2. Re-sign the operator-root catalogs at `data/oid-catalogs/` with your operator key:
   ```bash
   python scripts/sign_catalog.py --all
   ```
   This regenerates `data/oid-catalogs/cambium/pmp450i/{15.2.1,15.3.0,25.0.1,25.1.0,25.1}.json` against your `NORA_OID_CATALOG_SIGNING_KEY`.
3. Copy the regenerated envelopes to the deployed `NORA_OID_CATALOGS_PATH` (typically `/var/lib/nora/oid-catalogs/`).
4. Restart `nora-mcp`:
   ```bash
   sudo systemctl restart nora-mcp
   ```
5. Verify the boot accepted the new envelopes:
   ```bash
   sudo journalctl -u nora-mcp -n 50 | grep OidCatalogRegistry
   ```
   The registry reports `loaded_refs`; the five triples above must be present.

### For fresh installs

`scripts/install.sh` already calls `scripts/sign_catalog.py --all` with a fresh operator key. The shipped v2 envelopes in `src/nora/data/oid-catalogs/` verify against `BUILTIN_BASELINE_SIGNING_KEY` (placeholder). No operator action needed beyond the normal install flow.

### For tests / CI

The shipped `tests/test_oid_catalog_integration.py` already covers the five triples (`_RE_SIGNED_TRIPLES`). Tests use `BUILTIN_BASELINE_SIGNING_KEY` as the operator signing key — this is intentional and only safe in tests; production must use a real random key from `scripts/generate_signing_key.py`.

## Adding a new firmware baseline

1. Confirm the firmware with a live SNMP walk (the catalog `_provenance` block expects `verification_date` and the OIDs you observe).
2. Drop a new `data/oid-catalogs/sources/cambium/pmp450i/<firmware>.source.json` with the same shape as the existing sources (top-level `vendor`, `model`, `firmware`, optional `version`, `_provenance`, `oids`).
3. Sign: `python scripts/sign_catalog.py --vendor cambium --model pmp450i --firmware <firmware>` (this writes the signed envelope to `data/oid-catalogs/cambium/pmp450i/<firmware>.json`).
4. Copy the envelope to `src/nora/data/oid-catalogs/cambium/pmp450i/<firmware>.json` so it ships with the package.
5. Update `_RE_SIGNED_TRIPLES` in `tests/test_oid_catalog_integration.py`.
6. Add an entry to `data/oid-catalogs/CHANGELOG.md`.

## Common pitfalls

| Symptom | Cause | Fix |
|---------|-------|-----|
| `CatalogVerificationError: HMAC-SHA256 signature mismatch` | Catalog re-signed with a different key than `Settings.nora_oid_catalog_signing_key` | Re-run `scripts/sign_catalog.py --all` with the operator key in env |
| `CatalogVerificationError: missing signing key (NORA_OID_CATALOG_SIGNING_KEY is empty)` | `NORA_OID_CATALOG_SIGNING_KEY` not exported | `export NORA_OID_CATALOG_SIGNING_KEY=$(python scripts/generate_signing_key.py)` |
| `CatalogNotFoundError: major mismatch for ..., registered majors [15, 25]` | Device firmware on a major that has no registered envelope | Add a new `<firmware>.source.json` and re-sign |
| Migration tool still rejects frequencies as "not in the list" | Operator root still carries the v1 envelopes | Re-run the rollout plan above |
