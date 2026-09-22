# Issue #80 — `fix(migrate): snmp_migrate_radio_frequency executes dry-run only in production (missing writable client wiring)`

## Goal

Restore the Tier-1 wire path for `snmp_migrate_radio_frequency` and
`snmp_reboot_radio`: when an operator authorizes a migration with a valid HITL
token, the SET frames MUST emit on the wire against the AP radio, not silently
fall back to a typed dry-run result.

## Root cause (verified against the code)

The WU-3 (PR #44 follow-ups) dry-run gate was wired against a method name that
**does not exist** on either `SnmpClient` (read-only) or `WritableSnmpClient`
(write-capable). The actual contract for `WritableSnmpClient` is `set(oid, value)`
(`src/nora/drivers/snmp_pmp450i/v2c.py:252`, `v3.py:273`, `client.py:84`).

Affected call sites:

| File | Line | Current | Should be |
|---|---|---|---|
| `migrate.py` | 826 | `driver._client_factory(device)` (read-only) | `driver._writable_client_factory(device)` |
| `migrate.py` | 874 | `hasattr(client, "apply_oid")` | `hasattr(client, "set")` |
| `migrate.py` | 876 | `client.apply_oid(oid, mhz)` | `client.set(oid, kHz)` |
| `migrate.py` | 887 | `driver._client_factory(device)` (rollback) | `driver._writable_client_factory(device)` |
| `migrate.py` | 889 | `revert_client.apply_oid(oid, prior_carrier)` | `revert_client.set(oid, prior_carrier)` (prior_carrier is already kHz) |
| `reboot.py` | 240 | `driver._client_factory(device)` | `driver._writable_client_factory(device)` |
| `reboot.py` | 272 | `hasattr(client, "apply_oid")` | `hasattr(client, "set")` |
| `reboot.py` | 281 | `client.apply_oid(oid, value)` | `client.set(oid, value)` (no unit conversion; enum value) |

Test mock masking: `tests/snmp_pmp450i/test_migrate.py:157` defines a synthetic
`apply_oid()` method on the recording client, which makes the gate `True` in
tests while production (`V2CClient` does not implement `WritableSnmpClient`) is
always `False`.

## Scope decision

- **In scope (this PR):** migrate.py + reboot.py wiring + gate fix, kHz
  conversion, test mocks converted to `WritableSnmpClient`-shaped, regression
  tests for the dry-run seam (which stays as a feature, not deleted).
- **Out of scope (separate issue):** 3 GHz band-limit on
  `rank_clean_frequencies`. `_band_for_frequency` in
  `src/nora/drivers/snmp_pmp450i/band_plan.py` only knows 4.9/5.x bands; the
  `C030045A002A` (3 GHz CBRS) case requires either a new band entry or a band
  filter on the ranking function. Will create issue #81 with the evidence.

## Unit contract (decisión)

| Boundary | Unit |
|---|---|
| Public API (`target_frequency_mhz` parameter, intervention record) | MHz |
| Wire (`client.set(oid, value)` for `radioFreqCarrier`) | kHz |
| `prior_carrier` after `get_oid` (already a kHz int) | kHz (passthrough) |

Cambium WHISP-APS-MIB documents `whispApsRFConfigRadioEntry.radioFreqCarrier`
as INTEGER; the existing comment in `_band_crossing_from_prior` (migrate.py:580)
confirms "the prior_carrier read returns kHz". Same OID for read and SET →
same unit. Conversion lives at the call site (1 line, localized).

## Work-unit breakdown

### WU-1 — Fix the gate and wire the writable factory

TDD: write failing tests first.

1. `tests/snmp_pmp450i/test_migrate.py`
   - Replace `_SnmpClientForHost.apply_oid` with a proper
     `WritableSnmpClient`-shaped recording stub (defines `set`, records
     `(oid, value)` tuples, `get_oid` for sysDescr).
   - New test: `test_fetch_migrate_calls_set_with_khz_unit_on_migrate_carrier_frequency`
     asserting `set_calls` contains `(migrate_oid, int(round(target_mhz * 1000)))`.
   - New test: `test_fetch_migrate_rollback_watchdog_uses_writable_client_and_set`
     asserting the rollback path uses `_writable_client_factory` and `set()`.
2. `tests/snmp_pmp450i/test_reboot.py` (may not exist yet — create if needed)
   - Same WritableSnmpClient-shaped stub.
   - New test: `test_fetch_reboot_calls_set_on_reboot_oid`.
3. `src/nora/drivers/snmp_pmp450i/migrate.py`
   - Line 826: `driver._client_factory(device)` → `driver._writable_client_factory(device)`
   - Line 874: `hasattr(client, "apply_oid")` → `hasattr(client, "set")`
   - Line 876: `client.apply_oid(oid, target_frequency_mhz)` →
     `client.set(oid, int(round(target_frequency_mhz * 1000)))`
   - Line 887: `driver._client_factory(device)` → `driver._writable_client_factory(device)`
   - Line 889: `revert_client.apply_oid(...)` →
     `revert_client.set(migration_oids["migratePriorCarrierFrequency"], prior_carrier)`
   - Update module docstring (lines 28-31, 134-138) to drop `apply_oid` reference.
4. `src/nora/drivers/snmp_pmp450i/reboot.py`
   - Line 240: `driver._client_factory(device)` → `driver._writable_client_factory(device)`
   - Line 272: `hasattr(client, "apply_oid")` → `hasattr(client, "set")`
   - Line 281: `client.apply_oid(...)` → `client.set(...)`
   - Update module docstring (lines 36-39) to drop `apply_oid` reference.

Commit: `fix(snmp_pmp450i): wire WritableSnmpClient in migrate and reboot (closes #80)`

### WU-2 — kHz unit conversion regression tests + docs

1. `tests/snmp_pmp450i/test_migrate.py`
   - Pin the kHz contract: `set` receives `int(round(mhz * 1000))`.
   - Pin the rollback path: `set` receives the existing kHz value (passthrough).
2. ` OPERATIONS.md` — note the unit convention in the migration tool docs
   (search for "kHz" / "radioFreqCarrier").

Commit: `test(snmp_pmp450i): pin kHz wire unit for migrateCarrierFrequency (closes #80)`

### WU-3 — Band-limit 3 GHz (OUT OF SCOPE — opens issue #81)

Create issue #81 with evidence and link. No code change in this branch.

### WU-4 — E2E + observability (deferred)

After the wire path is fixed, add an integration smoke test that runs
`snmp_migrate_radio_frequency` against a fake WritableSnmpClient and asserts
(set_calls, dry_run=False, would_set=[]). Document the dry-run semantics in
OPERATIONS.md. Out of scope for this branch — opens issue #82.

## Risks and open questions

1. **Pre-1.0 callers of `_client_factory`** in migrate.py and reboot.py: there
   are no test fixtures other than the bug-masked ones, but a quick
   `grep -rn "_client_factory" tests/` should confirm nothing depends on the
   read-only behavior at these call sites.
2. **WritableSnmpClient type-narrowing**: `isinstance(client, WritableSnmpClient)`
   would be safer than `hasattr(client, "set")` for callers who pass arbitrary
   objects. Decision: keep `hasattr(client, "set")` to preserve the seam intent
   (mock without `set` → dry-run); tests will pin the contract.
3. **Existing in-flight migrations**: production currently always returns
   `dry_run: true` for this tool. After the fix, a fresh invocation will emit
   SET frames. Operators should be aware that any retry of an interrupted
   migration will now actually move the carrier. Mitigated by the rollback
   watchdog (now functional).
4. **Test flakiness on subprocess MCP boot**: tests/integration_boot.py pays
   ~15s cold start per test. The WritableSnmpClient unit tests are pure
   in-process, no subprocess, so no flakiness concern.

## Evidence trail

- Issue body: `https://github.com/alexandervazquez98/nora/issues/80`
- Prior art (correct pattern): `src/nora/drivers/snmp_pmp450i/spectrum.py:570`
  (`_writable_client_factory(device)`), `:239-241` (`client.set(...)`).
- Catalog notes: `data/oid-catalogs/cambium/pmp450i/25.0.1.json`,
  `notes` field documents `radioFreqCarrier` as the OID behind both
  `migrateCarrierFrequency` and `migratePriorCarrierFrequency`.
- Existing kHz evidence: `migrate.py:580-583` (`_band_crossing_from_prior`
  docstring) — "the prior_carrier read returns kHz".

## Convention notes

- Branch: `feat/issue-80-migrate-writable-wiring`
- Conventional Commits (per repo convention)
- Pre-commit: `gga` is the local hook; signed commits are enforced on the
  remote side. Always `git commit -S`.
- Tests: pytest with xdist; prefer in-process unit tests over subprocess boot.
