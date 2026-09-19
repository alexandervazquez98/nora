# Feature: Issue #62 — Real Cambium PMP 450i spectrum sweep protocol + per-SM community overrides

**Status**: 🚧 Planned — implementation not started.
**Branch**: `fix/issue-62-spectrum-and-multi-community` (from `origin/main` @ `fefd77d`, NOT yet created).
**Issue**: [#62](https://github.com/alexandervazquez98/nora/issues/62) — `fix(spectrum): replace synthetic mock OIDs (.221.1/.2/.3) with real Cambium PMP 450i sweep protocol` (with bundled per-SM community defect).

---

## Why this doc exists

GitHub issue #62 reports a **production-blocking** spectrum sweep failure on physical Cambium PMP 450i Access Points (firmware CANOPY 25.0.1 / 25.1). The shipped catalogs and helpers reference synthetic noise-floor scalars (`.221.1/.2/.3`) created during Slice 4 dev as snmpsim mocks — physical hardware does not implement them, so the MCP tool `snmp_run_spectrum_analysis` hard-fails with `noSuchName` on every production call.

The same issue bundles a **second, unrelated** defect: `snmp_migrate_radio_frequency` lacks any operator-facing parameter for per-SM community overrides, so heterogeneous multi-community sectors blow up mid-migration with a `SAFETY_ABORT` that leaves online SMs on the new frequency while the AP remains on the old one.

The two defects share no wire path or schema. We are implementing them as **two work-unit commits on a single feature branch** because they are both gated on the same production operator environment and the operator wants them shipped together; the ODD doc and the GitHub issue itself keep them logically separated so reviewers can read each one in isolation.

---

## Operator decisions (resolved 2026-09-19)

| # | Decision | Rationale |
|---|---|---|
| 1 | **Real sweep protocol applies to ALL shipped firmware envelopes** (15.2.1, 15.3.0, 25.0.1, 25.1.0, 25.1). | Operator confirmed 2026-09-19. No per-firmware carve-out; the SET/GET pattern is uniform across 15.x / 16.x / 20.x / 25.x Cambium firmware. |
| 2 | **Per-SM community keying: try IP first, fall back to LUID.** | Operator confirmed 2026-09-19: the `snmp_migrate_radio_frequency` tool is invoked with IP-targeted SETs, so IP is the natural key; LUID remains the inventory-side identifier. Resolution order: `ip_in_overrides → luid_in_overrides → inventory`. |
| 3 | **Branch from `origin/main` (`fefd77d`); do NOT mix with `feat/icmp-stability-probe` WIP.** | Issue #61 has uncommitted dirty work on the current branch; #62 is a separate production incident with its own review budget. |
| 4 | **Re-sign all 5 HMAC-signed catalogs after OID changes.** | Boot-time HMAC gate (`src/nora/drivers/oid_catalog.py:570`) fails closed on any tamper; the re-sign uses `Settings.nora_oid_catalog_signing_key`. New key ships with the feature; existing key stays valid for shipped envelopes only. |
| 5 | **Tier of `snmp_run_spectrum_analysis` stays Tier-1.** | Even though the real protocol emits a SET, the SET is bounded (duration + arm + start, no frequency change) and recovers to idle without side effects; the Tier-1 `operator_confirmed` gate already in place is sufficient. No HITL token required. |
| 6 | **Sweep timeout policy = hard fail with typed exception.** | Polling past `nora_spectrum_sweep_timeout_seconds` without reaching `0` (idle) raises `SpectrumSweepTimeout`; the result object still carries `scan_outcome="TIMEOUT"`, but the orchestrator receives the typed exception explicitly so it can retry or abort instead of silently consuming a partial sweep. |
| 7 | **Branch strategy = clean checkout from `origin/main`, leave `#61` WIP alone.** | `git stash` on the single dirty file `odd/tasks/issue-61-icmp-stability-probe.md` (status-log update post-merge of #61 PR1..PR3) so the new branch starts clean. The stash is tagged `issue-62-branch-prep` and lives at `stash@{0}` for the user to `git stash pop` on return to `feat/icmp-stability-probe`. |

---

## Out of scope (this feature)

- Real-time spectrum telemetry bin decoding (Cambium's per-bin RF noise histograms). Issue #62 only requires the SET-trigger + GET-poll loop; bin decoding is a future slice.
- Auto-promotion of `snmp_run_spectrum_analysis` to Tier-2 / HITL token. Per decision #5 we keep Tier-1.
- Removing the existing `nora_maintenance_window_*` guard on the spectrum path. It stays.
- A new `snmpsim/` data file. Tests will mock the new protocol at the `SnmpClient` boundary instead of at the snmpsim layer.

---

## Architectural decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | **Spectrum OID schema change**: drop `spectrumNoiseFloorA/B/C`, `spectrumChannelRank` (synthetic `.221.1/.2/.3/.4`). Add `spectrumScanDuration` (`.220.0`, Integer, seconds) and reuse `spectrumScanStatus` (`.221.0`, Integer, status code). | Matches Cambium WHISP-BOX-MIBV2-MIB: `whispBoxSpectrumScanDuration` and `whispBoxSpectrumScanAction`. Status code semantics: `0` = idle, `1` = running, `8` = armed. |
| 2 | **New typed result `SpectrumSweepResult`** carries `device_id`, `scan_started_at`, `scan_completed_at`, `sweep_duration_seconds`, `final_status`, `ranked_clean_frequencies` (kHz), `noise_floor_dbm` (freq-kHz → dBm), `scan_outcome` (`COMPLETED` / `TIMEOUT` / `ABORTED`). | Operator needs the timeline metadata to reason about partial sweeps. The legacy `SpectrumAnalysis` model is replaced — no dual-name alias, since the synthetic OIDs are gone. |
| 3 | **Sweep sequence (real protocol)**: SET `.220.0 = duration` → SET `.221.0 = 8` (arm) → SET `.221.0 = 1` (start) → poll `.221.0` every `poll_interval` until `0` (idle) or `timeout` elapses → return result. | Matches Cambium MIB semantics; preserves the existing Tier-1 `operator_confirmed` gate and the `nora_maintenance_window_*` guard. |
| 4 | **New config knobs**: `nora_spectrum_sweep_duration_seconds` (default 15), `nora_spectrum_sweep_poll_interval_seconds` (default 1.0), `nora_spectrum_sweep_timeout_seconds` (default 60). | Mirrors the ICMP probe config layout (`nora_icmp_*`). Hardcoded values stay out of the helper. |
| 5 | **Per-SM community resolution**: `fetch_migrate` accepts `sm_communities: dict[str, str] \| None = None` (keyed by IP). New helper `_resolve_sm_community(device, override_ip, override_luid, inventory)` returns `(community, source)` where `source ∈ {OVERRIDE_IP, OVERRIDE_LUID, INVENTORY}`. | Decision #2 + auditability: the pre-flight report now carries `community_source` per SM so the operator sees which credential was used. |
| 6 | **MCP tool signature**: `snmp_migrate_radio_frequency(device_id, approval_token, target_frequency_mhz, sm_communities: dict[str, str] \| None = None)`. | `None` and absent → legacy behaviour (inventory communities only). Non-empty dict → IP-first override with LUID fallback per decision #2. |

---

## Work units

Each WU closes with one work-unit commit on the feature branch. WUs run sequentially because each unblocks tests for the next.

### WU-1: OID catalog schema migration (spectrum) — all 5 envelopes + boot-time gate

**Touch**:
- `data/oid-catalogs/sources/cambium/pmp450i/*.source.json` (5 files)
- `data/oid-catalogs/cambium/pmp450i/*.json` (5 signed envelopes, regenerated via the signer)
- `src/nora/data/oid-catalogs/cambium/pmp450i/*.json` (5 built-in baseline catalogs, regenerated via the signer — ships in every install via `importlib.resources`)
- `scripts/sign_catalog.py` (TOOLS_V1 update)
- `src/nora/drivers/oid_catalog.py` (boot-time `_REQUIRED_OIDS_BY_VENDOR_MODEL` table)
- `tests/test_sign_catalog.py` (HMAC canonicalisation contract test)
- `tests/test_oid_catalog_integration.py` (hermetic fixture + tools map)
- `tests/test_oid_catalog.py` (its `_SAMPLE_CATALOG_PAYLOAD` fixture)
- `tests/conftest.py` (cross-test hermetic catalog fixture)
- `tests/test_snmp_spectrum.py` and any other spectrum-related integration test → **skip with `@pytest.mark.skip(reason="...WU-2 pending")`** until WU-2 lands; re-enable in WU-2.

**Change**:
- Remove `spectrumNoiseFloorA`, `spectrumNoiseFloorB`, `spectrumNoiseFloorC`, `spectrumChannelRank` from every `oids` map and every `snmp_run_spectrum_analysis` tool list.
- Add `spectrumScanDuration` mapped to `1.3.6.1.4.1.161.19.3.3.2.220.0`.
- Rename `spectrumScanStatus` to `spectrumScanAction` (same dotted OID `.221.0`; new name reflects Cambium MIB and SET/GET semantics).
- Update boot-time `_REQUIRED_OIDS_BY_VENDOR_MODEL[("cambium", "pmp450i")]` to require the new OID names.
- Re-sign every envelope (operator root + built-in baseline) with HMAC-SHA256 against the canonicalised OID map.

**Tests**:
- `pytest tests/test_sign_catalog.py tests/test_oid_catalog_integration.py tests/test_oid_catalog.py` — must pass.
- `pytest tests/` (full suite) — must pass except for the spectrum-related tests skipped with `@pytest.mark.skip(reason="WU-2 pending")`.
- Built-in baseline catalogs are NOT out-of-band in the sense the prior ODD doc claimed — they live in `src/nora/data/oid-catalogs/` and ARE loaded at every install via `importlib.resources`. Re-signing them in WU-1 is required because the gate MUST stay consistent across both roots or every boot path crashes.

### WU-2: Spectrum helper rewrite (`src/nora/drivers/snmp_pmp450i/spectrum.py`)

**Touch**:
- `src/nora/drivers/snmp_pmp450i/spectrum.py` (full rewrite of `fetch_spectrum`)
- `src/nora/config.py` (3 new `nora_spectrum_*` knobs + validator)
- `src/nora/server.py` (`snmp_run_spectrum_analysis` signature — accept `sweep_duration_seconds: int | None = None`)
- `tests/test_snmp_spectrum.py` (rewrite against the new protocol; **un-skip the WU-1 `@pytest.mark.skip` markers here**)
- `tests/snmp_pmp450i/test_spectrum.py` (new or rewritten)

**Change**:
- Replace `SpectrumAnalysis` with `SpectrumSweepResult` (per decision #2).
- `fetch_spectrum(...)` performs the SET/GET poll loop from decision #3.
- New helpers: `_arm_sweep(client, duration_oid, action_oid, duration)`, `_poll_sweep_status(client, action_oid, poll_interval, timeout) -> int`.
- Tier-1 `operator_confirmed` gate + maintenance-window guard preserved.

**Tests**: `tests/snmp_pmp450i/test_spectrum.py` covers happy path, timeout, ARM-only (no START), maintenance-window rejection, Tier-1 refusal. `test_oid_catalog_integration.py` covers the new required-OID set + HMAC verification.

### WU-3: Tests for spectrum sweep + conftest alignment

**Touch**:
- `tests/conftest.py` (existing `spectrumNoiseFloorA/B/C` aliases — drop or align with new schema)
- `tests/snmp_pmp450i/test_spectrum.py` (full coverage)

**Change**:
- Drop the legacy 4 OIDs from the conftest catalog stub; add `spectrumScanDuration` and `spectrumScanAction` aliases.
- New tests use a fake `SnmpClient` that records SET/GET sequence and lets the test inject the polled-status sequence.

### WU-4: Per-SM community overrides — public API

**Touch**:
- `src/nora/server.py` (`snmp_migrate_radio_frequency` signature gains `sm_communities`)
- `src/nora/drivers/snmp_pmp450i/migrate.py` (`fetch_migrate` signature + `_resolve_sm_community` helper + `_validate_sm_communities` consumes overrides)

**Change**:
- New helper `_resolve_sm_community(sm_device, luid, override_ip, override_luid) -> (community_str, source)` where `source ∈ {OVERRIDE_IP, OVERRIDE_LUID, INVENTORY}`.
- `_validate_sm_communities` accepts a `community_resolver` callable so the pre-flight honours overrides; the `SmPreFlightResult` model gains a `community_source` field.
- `MigrationResult` (return shape) carries an aggregate `sm_community_overrides_used: int` so the orchestrator / audit trail sees how many SMs were migrated with non-inventory credentials.

**Tests**: `tests/snmp_pmp450i/test_migrate.py` covers IP override, LUID override, mixed dict, invalid override IP (raises `InvalidCommunity`), `None`/`{}` legacy behaviour unchanged.

### WU-5: Config + docs for `sm_communities`

**Touch**:
- `src/nora/config.py` (no new persistent knob needed — the dict comes via the MCP tool call, not env)
- `docs/tool_specs/snmp_migrate_radio_frequency.md` (or equivalent; verify location) — document the new optional parameter
- `INSTALL.md` / `OPERATIONS.md` — only if the existing multi-community section needs cross-linking

**Change**: docs-only update describing the new `sm_communities` parameter shape and the resolution order.

### WU-6: End-to-end verification

**Touch**: none — verification only.

**Steps**:
- `pytest tests/snmp_pmp450i/test_spectrum.py tests/snmp_pmp450i/test_migrate.py tests/test_oid_catalog_integration.py` — must pass.
- `ruff check src/nora tests` and `mypy src/nora` — must pass.
- Catalog boot HMAC check: `python -c "from nora.drivers.oid_catalog import OidCatalogRegistry; OidCatalogRegistry.from_settings(settings).verify_all()"` — must succeed for all 5 envelopes.

---

## Risk register

| Risk | Mitigation |
|---|---|
| Catalog HMAC re-signing changes the published baseline → downstream forks / packaged deployments break. | Re-sign with the same `nora_oid_catalog_signing_key` shipped with v0.3.1; existing keys still verify. |
| Real sweep protocol mutates radio state (arm/start) without a frequency change → operator concern. | Tier-1 `operator_confirmed` gate preserved; sweep only emits SET to the spectrum-scan scalars, never to `radioFreqCarrier` / `migrateCarrierFrequency`. |
| `sm_communities` parameter leaks credentials in audit logs / intervention records. | `SmPreFlightResult.community_source` carries the source label, never the string value; intervention writer is unchanged (still emits `device_id` only). |
| Two unrelated defects on one branch → confusing PR review. | ODD doc keeps WU-1..3 (spectrum) and WU-4..5 (multi-community) logically separated; PR description mirrors the two-bullet structure. |

---

## Related work (non-blocking)

- `odd/tasks/multi-community-migration-and-band-reboot.md` (planned, branch not created) — the original multi-community + band-reboot scope. This issue #62 fix ships the **`sm_communities` operator-facing parameter** only; the band-reboot tool + band-crossing signal are still pending in that task.
- `odd/tasks/pmp450i-25.0.1-hardening.md` (closed via PR #59) — the catalog baseline this work builds on.
