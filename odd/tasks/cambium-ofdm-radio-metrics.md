# Feature: Cambium PMP 450i OFDM radio metrics & SM diagnostics OID correction (closes #54)

**Branch**: `fix/cambium-ofdm-radio-metrics`
**Source**: GitHub issue #54 — live-wire audit of telemetry against PMP 450i firmware 25.0.1 / 25.1.0
**Mode**: Three work-unit commits stacked on the fix branch (catalogs, driver, REQUIRED_OIDS+tests)
**Delivery**: Single PR after all three WUs land and the catalog HMAC gate is green

---

## Scope

Correct three classes of catalog/driver bugs surfaced by live-wire SNMP queries against PMP 450i hardware:

1. **FSK vs OFDM mismatch** — `smJitter: 1.3.6.1.4.1.161.19.3.1.4.1.22` (`linkAveJitter`) is documented as FSK-only and returns `noSuchInstance` on OFDM/MIMO hardware (PMP 450i).
2. **Engineering-only / tabular misclassification** — `signalStrengthTx: 1.3.6.1.4.1.161.19.3.1.4.1.89` (`maxSMTxPwr`) is engineering-only and lives in the per-LUID `whispLinkEntry` table; GET as scalar returns empty.
3. **Driver indexing bug** — `fetch_sm_detailed_diagnostics` queries per-LUID columns as `.0` (scalar) instead of appending `.<luid>`.

### Non-goals

- No new tool surface — every fix is in-place on existing MCP tools.
- No protocol change to MCP envelope; field renames are internal-only on the typed `SmDetailedDiagnostics` model.
- No version bump; release is a separate user decision.
- No firmware-parity regression — the v2 envelope stays backwards-compatible with the v2 schema (`REQUIRED_OIDS_BY_VENDOR_MODEL` audit must pass).

---

## Affected firmware majors

The bug is identical across all 5 PMP 450i catalogs (15.2.1, 15.3.0, 25.0.1, 25.1, 25.1.0). All five are touched:

- `data/oid-catalogs/sources/cambium/pmp450i/*.source.json` (5 files) — source of truth
- `data/oid-catalogs/cambium/pmp450i/*.json` (5 files) — HMAC-signed runtime envelope
- `src/nora/data/oid-catalogs/cambium/pmp450i/*.json` (5 files) — built-in baseline shipped in the wheel

15 catalog files total. All HMAC signatures re-issued via `scripts/sign_catalog.py --all`.

---

## OID schema decisions

### Sector-level (`snmp_get_pmp450i_radio_metrics`)

| Action | Old | New | Rationale |
|---|---|---|---|
| **Rename** | `signalStrengthTx: .89.0` (broken `maxSMTxPwr`) | `eirp: .306.0` (`whispBoxActiveEIRP`) | Sector-level active EIRP, populated on production firmware |
| **Add** | (none) | `activeTxPowerDbh: .233.0` (`whispBoxActiveTxPowerInHundredthsDbm`) | Sector-level active TX power in hundredths of dBm (int) |
| **Keep** | `transmitPower: .232.0` (`whispBoxActiveTxPower`) | unchanged — DisplayString form | Already correct; used by `snmp_get_ap_summary` |
| **Keep** | `ssr: .86.0` | unchanged | Per-LUID SSR; the sector-level SSR OID is not exposed in this tool |
| **Drop from tool envelope** | `signalStrengthTx` | replaced by `eirp` | Field rename on the tool |

### Per-LUID / SM-level (`snmp_get_sm_detailed_diagnostics`)

| Action | Old | New | Rationale |
|---|---|---|---|
| **Drop** | `smJitter: .22` (`linkAveJitter`) | — | FSK-only, broken on OFDM |
| **Drop** | `smTxLevel: .89` (`maxSMTxPwr`) | — | Engineering-only + tabular, same bug as sector-level |
| **Keep** | `smCinr: .74` (`linkRadioAggrSmVCalculatedSnr`) | unchanged | Per-LUID vertical CINR — needs `.<luid>` index |
| **Keep** | `smRxLevel: .34` (`linkRadioAggrSmVRecPwr`) | unchanged | Per-LUID vertical Rx power — needs `.<luid>` index |
| **Keep** | `smRetransmits: .150` (`linkRetransmittedFragCount`) | unchanged | Per-LUID retransmits |
| **Add** | (none) | `smSnrH: .84.0` (`linkRadioAggrSmHCalculatedSnr`) | Per-LUID horizontal CINR/SNR |
| **Add** | (none) | `ssrLink: .86.0` (`linkRadioAggrSignalStrengthRatio`) | Per-LUID SSR (column) |

All per-LUID columns queried in `fetch_sm_detailed_diagnostics` are dispatched with `.<luid>` appended (driver-side fix; the catalog OIDs keep the canonical `.0` shape for the SM-table walk to derive column indexes).

---

## Work units

### WU-A — Catalog updates + HMAC re-sign

**Symptom fixed**: All 5 PMP 450i v2 catalogs ship broken OIDs (`smJitter`, `signalStrengthTx`, `smTxLevel`). The sector-level `eirp` and the horizontal/SSR per-LUID columns are missing entirely.

**Fix**:
- Edit the 5 `*.source.json` files: drop `smJitter`, `smTxLevel`, `signalStrengthTx`; add `eirp`, `activeTxPowerDbh`, `smSnrH`, `ssrLink`.
- Re-run `scripts/sign_catalog.py --all` to re-issue HMACs (5 envelopes + 5 built-in baselines, copied atomically).
- Update `sign_catalog.py:TOOLS_V1` so `snmp_get_pmp450i_radio_metrics` lists `eirp` (not `signalStrengthTx`) and `snmp_get_sm_detailed_diagnostics` lists the new OID set.

**Files**:
- `data/oid-catalogs/sources/cambium/pmp450i/{15.2.1,15.3.0,25.0.1,25.1,25.1.0}.source.json` — OID updates
- `data/oid-catalogs/cambium/pmp450i/{15.2.1,15.3.0,25.0.1,25.1,25.1.0}.json` — regenerated envelopes
- `src/nora/data/oid-catalogs/cambium/pmp450i/{15.2.1,15.3.0,25.0.1,25.1,25.1.0}.json` — regenerated baselines
- `scripts/sign_catalog.py` — TOOLS_V1 OID-name map

**Evidence gate**:
- `python scripts/sign_catalog.py --all` exits 0; new HMACs in the 5 signed envelopes.
- `OidCatalogRegistry.verify_all` (or `python -m nora.drivers.oid_catalog`) loads the 5 catalogs without `CatalogVerificationError`.

### WU-B — Driver per-LUID indexing + typed model update

**Symptom fixed**: `subscribers.fetch_sm_detailed_diagnostics` queries the catalog OID literally (e.g. `1.3.6.1.4.1.161.19.3.1.4.1.22.0`) for per-LUID columns. The driver must strip `.0` and append `.<luid>` for every `whispLinkEntry` column.

**Fix**:
- `subscribers.py`:
  - Drop `smJitter` from `SM_DIAGNOSTICS_OID_NAMES`.
  - Add `smSnrH`, `ssrLink` (and document that `smCinr`, `smRxLevel`, `smRetransmits` are already per-LUID).
  - Add `_resolve_sm_diagnostics_oids_with_luid(catalog, luid) -> dict[str, str]` that returns `oid_name → "<base>.<column>.<luid>"` for every per-LUID column.
  - Rewrite `fetch_sm_detailed_diagnostics` to call `client.get_oid(oid_with_luid)` per field.
- `SmDetailedDiagnostics` typed model:
  - Drop `jitter_ms: int | None`.
  - Add `snr_h_db: int | None` and `ssr_link_db: int | None`.
  - Drop `tx_level_dbm: int | None` (broken OID — `maxSMTxPwr` engineering-only).
- `server.py:snmp_get_sm_detailed_diagnostics` docstring: reflect the new field set.

**Files**:
- `src/nora/drivers/snmp_pmp450i/subscribers.py` — drop `smJitter`, add `smSnrH` + `ssrLink`, append `.<luid>` per OID, update `SmDetailedDiagnostics` model
- `src/nora/server.py` — tool docstring update only (no logic change)

**Evidence gate**:
- New RED-first test: `fetch_sm_detailed_diagnostics` against a fake client where `get_oid` returns a known int per per-LUID form; assert every field is the int (no None for reachable SMs).
- New RED-first test: OID name `smJitter` is NOT in `SM_DIAGNOSTICS_OID_NAMES` (compile-time import).
- Existing `tests/test_oid_catalog.py` GREEN (no regressions on SM-table walk).

### WU-C — REQUIRED_OIDS, test fixtures, sign-script TOOLS_V1 lockstep

**Symptom fixed**: After WU-A drops `signalStrengthTx`, `smJitter`, `smTxLevel` from the catalogs, the `_REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]` set in `oid_catalog.py:65` and the global `REQUIRED_OIDS` alias still reference the old names. Catalog verification gate fails on every catalog. Test fixtures in `tests/conftest.py`, `tests/test_oid_catalog.py`, `tests/test_oid_catalog_integration.py`, `tests/test_driver_airgap.py`, `tests/test_snmp_migrate.py`, `tests/test_driver_interface.py`, `tests/test_driver_snmpsim_v2c.py` still hardcode the old OIDs.

**Fix**:
- `oid_catalog.py`:
  - Update `_REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]`:
    - Drop `smJitter`, `signalStrengthTx`.
    - Add `eirp`, `smSnrH`, `ssrLink`.
  - Update module-level `REQUIRED_OIDS` alias (used by `Pmp450iDriver._fetch_all` for `fetch_radio_metrics`):
    - Drop `signalStrengthTx`.
    - Add `eirp`.
- Update every test fixture listed above to use the new OID names where the legacy OIDs were hardcoded.

**Files**:
- `src/nora/drivers/oid_catalog.py` — REQUIRED_OIDS table updates
- `tests/conftest.py` — fixture update
- `tests/test_oid_catalog.py` — fixture update
- `tests/test_oid_catalog_integration.py` — fixture update
- `tests/test_driver_airgap.py` — fixture update
- `tests/test_snmp_migrate.py` — fixture update
- `tests/test_driver_interface.py` — fixture update
- `tests/test_driver_snmpsim_v2c.py` — fixture update

**Evidence gate**:
- `pytest tests/ -x` exits 0.
- `OidCatalogRegistry.verify_all` loads all 5 PMP 450i catalogs.

---

## Sequencing & commits

| Order | WU | Commit title | Estimated diff |
|----:|-----|--------------|----------------|
| 1 | WU-A | `fix(cambium/pmp450i): correct OFDM radio metrics & SM diagnostics OIDs across 5 catalogs (closes #54)` | +60 / -40 LOC across 15 JSON files + sign |
| 2 | WU-B | `fix(cambium/pmp450i/subscribers): append .<luid> to per-LUID column queries; drop jitter_ms/tx_level_dbm` | +40 / -25 LOC |
| 3 | WU-C | `fix(cambium/pmp450i): update REQUIRED_OIDS table and test fixtures for #54 OID renames` | +30 / -30 LOC |

Total estimated diff: ~130 LOC + ~15 catalog file rewrites. Within review-workload limits for a single PR.

---

## Risks & live-system rules

- HMAC re-signing touches the 10 signed envelopes (5 runtime + 5 baseline). The baseline key is the wheel-shipped key — the runtime root uses the env-injected `NORA_OID_CATALOG_SIGNING_KEY`. Both must succeed.
- Breaking change on `snmp_get_pmp450i_radio_metrics`: `signalStrengthTx` is dropped in favour of `eirp`. The MCP envelope field name changes — consumers reading the field by name must update. Documented in commit message + release notes (if/when a release is cut).
- No commits will be made without going through work-unit-commit discipline (one WU = one commit, Conventional Commits title, evidence gate per WU).
- No merge to main, no tag, no release. The user owns merge.
- After all three WUs land, re-run the full pytest suite + ruff + mypy + coverage to confirm no regression. Run `OidCatalogRegistry.verify_all` once to confirm the catalog gate is green.
- RDD (Receipt-Driven Development): follow the gentle-pi review pipeline when the user enables it.

---

## Confirmed decisions (2026-09-19)

1. **Scope**: All 5 PMP 450i catalogs (`15.2.1`, `15.3.0`, `25.0.1`, `25.1`, `25.1.0`). The bug is firmware-major-agnostic and the 5 catalogs share identical OID sets — fixing only 25.x would leave 15.x consumers exposed.
2. **`ssr`**: kept in the sector-level catalog as a name (still points at `.86.0` for backwards compat) and re-introduced as `ssrLink` (per-LUID) in SM diagnostics. The name collision is documented; no merge needed.
3. **`smTxLevel`**: dropped entirely (engineering-only + tabular, returns empty on production firmware). `tx_power` for an SM is not exposed by Cambium's WHISP-APS-MIB as a populated column.