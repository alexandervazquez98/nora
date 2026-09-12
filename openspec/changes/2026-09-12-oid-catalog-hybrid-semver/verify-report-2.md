# Verify Report: 2026-09-12-oid-catalog-hybrid-semver (PR 2)

## Summary

- Branch: `feat/p2-semver-resolution` @ `98263ed42b755ec0d9fbafabaedbcff94abe1c84`
- Verification date: 2026-09-12
- PR scope: PR 2 (`p2-semver-resolution`) — semver-aware `resolve()`, strict-major hard-fail, minor descending fallback + literal telemetry warning, locking fixture reissued.
- Diff base: `055fb97` (PR 1 merge on `main`) → `HEAD` (PR 2 stack)
- Outcome: **PASS**

## Toolchain

| Check | Result | Detail |
|-------|--------|--------|
| `pytest` | PASS | 298 passed, 2 skipped, 0 failed (300 collected; 2 pre-existing skips unchanged from PR 1) |
| `ruff check .` | PASS | All checks passed |
| `mypy --strict src/nora` | PASS | Success: no issues found in 27 source files |
| `ruff format --check .` | PASS | 62 files already formatted |

### Toolchain evidence

- `uv run pytest` → `298 passed, 2 skipped, 1 warning in 83.82s (0:01:23)`
- The single warning is the pre-existing `python -m nora` deprecation in `test_server.py` (unchanged by this PR).
- No new warnings introduced by PR 2.

## Spec Scenarios (PR 2)

| Scenario | Result | Test |
|----------|--------|------|
| `Semver-Aware Firmware Resolution > exact match returns without warning` | PASS | `TestSemverResolution::test_exact_match_returns_without_warning` (WU 2.1) |
| `Semver-Aware Firmware Resolution > pre-release request matches the bare version` | PASS | `TestSemverResolution::test_pre_release_request_matches_bare_version` (WU 2.1) |
| `Strict-Major Hard Fail > major mismatch raises a typed exception` | PASS | `TestSemverResolution::test_major_mismatch_raises_typed_exception` (WU 2.2) |
| `Minor Descending Fallback With Literal Warning > minor mismatch returns closest lower minor with literal warning` | PASS | `TestSemverResolution::test_minor_mismatch_returns_closest_lower_minor_with_literal_warning` (WU 2.3) |

### Evidence

- `tests/test_oid_catalog.py::TestSemverResolution::test_exact_match_returns_without_warning` → 1 passed in 0.51s
- `tests/test_oid_catalog.py::TestSemverResolution::test_pre_release_request_matches_bare_version` → 1 passed in 0.39s
- `tests/test_oid_catalog.py::TestSemverResolution::test_major_mismatch_raises_typed_exception` → 1 passed in 0.55s
- `tests/test_oid_catalog.py::TestSemverResolution::test_minor_mismatch_returns_closest_lower_minor_with_literal_warning` → 1 passed in 0.67s

## ADR #17 Named Tests (PR 2 coverage)

| Test | Result | Implementation |
|------|--------|----------------|
| 5. Firmware idéntico al catálogo: sin warning | PASS | `TestSemverResolution::test_exact_match_returns_without_warning` asserts `all(r.levelno < logging.WARNING for r in caplog.records)` against the `nora.drivers.oid_catalog` logger. |
| 6. Minor mismatch (15.2.1 vs 15.3.0): fallback al más cercano + warning literal `"OID catalog fallback: requested X, using Y (minor mismatch)"` | PASS | `TestSemverResolution::test_minor_mismatch_returns_closest_lower_minor_with_literal_warning` asserts `caplog.records[0].message == "OID catalog fallback: requested 15.3.1, using 15.3.0 (minor mismatch)"` exact byte-match. |
| 7. Major mismatch (15.x vs 16.x): hard fail con excepción tipada nombrando el major mismatch | PASS | `TestSemverResolution::test_major_mismatch_raises_typed_exception` raises `CatalogNotFoundError`; message contains both `"16"` (requested) and `"15"` (registered). |
| 8. Múltiples menores firmados: usar el menor más cercano determinístico | PASS | Ad-hoc determinism verification (registry `(15.2.1, 15.2.3, 15.3.0)`): `15.2.5` → `15.2.3`; `15.3.1` → `15.3.0`; `15.2.99` → `15.2.3`; `15.2.3-rc.1` → `15.2.3` (pre-release strip still applies). All passes. |

### Test 8 ad-hoc trace (deterministic minor descending)

```
15.2.5       → 15.2.3  (closest lower minor)   PASS
15.3.1       → 15.3.0  (closest lower minor)   PASS
15.3.0       → 15.3.0  (exact match, no warn)  PASS
15.2.3-rc.1  → 15.2.3  (pre-release stripped)  PASS
15.2.99      → 15.2.3  (closest lower minor)   PASS
```

## Design Decisions Compliance

| Decision | Implemented? | Evidence |
|----------|--------------|----------|
| **D2** — `packaging.version.Version` + `.base_version` strip pre-release + build metadata | PASS | `src/nora/drivers/oid_catalog.py:46` (`from packaging.version import InvalidVersion, Version`); lines 154-163 use `Version(firmware).base_version` for both request and registered firmware. Pass 2 strips `-rc.1` / `+build.5` before compare. |
| **D3** — Reuse `CatalogNotFoundError(ref)` (no new subclass); message names both majors | PASS | `src/nora/drivers/oid_catalog.py:189-194` re-raises `CatalogNotFoundError(ref)` with `exc.args = (f"major mismatch for {ref}: requested major {req_version.major}, registered majors {registered_majors}",)` — both `req_version.major` and `registered_majors` (sorted list) appear. No new exception subclass introduced (`CatalogNotFoundError` is the existing class at `src/nora/drivers/exceptions.py:67`). |
| **D6** — `logger.warning("OID catalog fallback: requested %s, using %s (minor mismatch)", req, chosen)` literal | PASS | `src/nora/drivers/oid_catalog.py:220-224`: `logger.warning("OID catalog fallback: requested %s, using %s (minor mismatch)", firmware, chosen_catalog.firmware,)`. Exact format string and argument order match D6. Logger name is `nora.drivers.oid_catalog` (line 52); routes through root → `configure_logging()` stderr handler (`src/nora/server.py:48-66`). |

## Bit-Identical Operator-Only Path Preservation

| Check | Result | Detail |
|-------|--------|--------|
| R1-R7 verbatim green | PASS | `test_resolve_reads_pinned_catalog_path`, `test_resolve_under_explicit_path`, `test_valid_signed_catalog_verifies`, `test_tampered_catalog_raises_catalog_verification_error`, `test_missing_key_raises_catalog_verification_error`, `test_key_rotation_invalidates_previously_signed_catalog`, `test_missing_required_oid_raises_catalog_verification_error`, `test_catalog_files_contain_only_public_names_and_dotted_oids`, `test_oid_catalog_module_is_network_free` — all 9 R-tests pass. |
| 9 `TestMultiRoot` + 5 `TestSignCatalog` (PR 1) green | PASS | `uv run pytest tests/test_oid_catalog.py::TestMultiRoot tests/test_sign_catalog.py -v` → 14 passed. |
| Operator-only path equivalence (Pass 1 short-circuit) | PASS | Pass 1 (`src/nora/drivers/oid_catalog.py:142-143`) returns `self._catalogs[ref]` immediately when the exact `(vendor, model, firmware)` key is present, identical to PR 1's pre-PR2 lookup semantics. Operator-only fixtures (no built-in, single root) hit Pass 1 and never reach Pass 2-4. |

### Operator-only path verification

```
uv run pytest tests/test_oid_catalog.py -k "not TestSemverResolution and not test_unknown_firmware" -v
→ 19 passed, 5 deselected in 0.89s
```

## Locking Fixture Reissue

| Check | Result | Detail |
|-------|--------|--------|
| `test_unknown_firmware_raises_catalog_not_found` PASS | PASS | `uv run pytest tests/test_oid_catalog.py::test_unknown_firmware_raises_catalog_not_found -xvs` → 1 passed in 0.50s. |
| Message names both majors | PASS | Fixture now writes a `15.x` catalog so the strict-major branch has registered majors to name. The assertion verifies `"99"` (requested major) and `"15"` (registered major) BOTH appear in the exception message. Per spec migration note in `oid-catalog/spec.md`: the fixture now requests `99.0.0` against only `15.x` catalogs and still raises a typed `CatalogNotFoundError`. |
| Per-Firmware Pin wording removed | PASS | `R2 — Per-firmware pin (fail-closed)` docstring header replaced with `Strict-Major Hard Fail — locking fixture reissued under PR 2 (ADR #17 P2)`. Spec REMOVED the `Per-Firmware Pin` requirement. |

## Cross-File Consistency

| Check | Result | Detail |
|-------|--------|--------|
| Scope creep detected (tests outside `test_oid_catalog.py`) | PASS (none) | `git diff 055fb97..HEAD --name-only` → only `src/nora/drivers/oid_catalog.py` and `tests/test_oid_catalog.py`. No changes to `test_server.py`, `test_integration.py`, `test_integration_boot.py`, `test_main_alias.py`, or any other test file. |
| Unlisted files modified | PASS (none) | PR 2's `tasks.md` lists `src/nora/drivers/oid_catalog.py` and `tests/test_oid_catalog.py` for WUs 2.1, 2.2, 2.3, 2.4. The actual diff matches this list exactly. No unlisted files modified. |
| Planning artifacts touched | PASS (none) | `git status` shows `openspec/changes/2026-09-12-oid-catalog-hybrid-semver/{design.md,proposal.md,spec.md,tasks.md,verify-report.md}` untouched (only this `verify-report-2.md` will be added). |
| Untracked artifacts (informational) | NOTE | `.pi/` and `data/devices-tmp.yaml` are untracked — pre-existing dev artifacts, not introduced by PR 2. Out of scope. |

## Findings

### CRITICAL (blockers — must fix before merge)

(none)

### WARNING (must address but not blocking)

(none)

### SUGGESTION (nice to have)

1. **Test 8 (deterministic multi-minor) is only verified via ad-hoc trace, not a named regression test.** The 4 named `TestSemverResolution` cases cover exact / pre-release / major / single-minor-fallback, but a test that exercises the `(15.2.1, 15.2.3, 15.3.0)` fixture with three different request firmwares would lock determinism in the suite. (Not blocking; the implementation is verified by the ad-hoc trace in this report and the sorting logic at `oid_catalog.py:218` is straightforward. PR 3 or a follow-up may add this.)
2. **Pre-release build-metadata path is undocumented in tests.** Pass 2 strips `+build.5` via `Version(...).base_version`, but no named test exercises the `+build` path. The `Version("15.2.1+build.5").base_version == "15.2.1"` behaviour is covered implicitly via `packaging>=24.0`. (Not blocking.)

## Verdict

**PR 2 is READY for merge.**

- Toolchain: green (298 passed, 2 skipped, ruff/mypy/format clean).
- Spec scenarios (4): all green.
- ADR #17 P2 named tests (4): all green.
- Design decisions (D2, D3, D6): all implemented as specified.
- Bit-identical operator-only path: preserved (Pass 1 short-circuit at `oid_catalog.py:142-143`).
- Locking fixture reissue: passes; message names both majors; spec migration note satisfied.
- Cross-file consistency: no scope creep, no unlisted files, planning artifacts untouched.

## Next Steps

- Proceed to push `feat/p2-semver-resolution` and open PR → `main`.
- After merge, the change enters the archive phase (run `sdd-archive` to sync delta specs).
- If a follow-up PR adds the multi-minor regression test, no spec/design update is required (same scenarios covered).

## Verification Command Reference

```bash
# Toolchain
uv run pytest                                              # 298 passed, 2 skipped
uv run ruff check .                                        # All checks passed
uv run mypy --strict src/nora                              # Success: no issues found in 27 source files
uv run ruff format --check .                               # 62 files already formatted

# Spec scenarios (PR 2)
uv run pytest tests/test_oid_catalog.py::TestSemverResolution::test_exact_match_returns_without_warning -xvs
uv run pytest tests/test_oid_catalog.py::TestSemverResolution::test_pre_release_request_matches_bare_version -xvs
uv run pytest tests/test_oid_catalog.py::TestSemverResolution::test_major_mismatch_raises_typed_exception -xvs
uv run pytest tests/test_oid_catalog.py::TestSemverResolution::test_minor_mismatch_returns_closest_lower_minor_with_literal_warning -xvs

# Operator-only path (PR 1 preserved)
uv run pytest tests/test_oid_catalog.py -k "not TestSemverResolution and not test_unknown_firmware" -v
uv run pytest tests/test_oid_catalog.py::TestMultiRoot tests/test_sign_catalog.py -v

# Locking fixture
uv run pytest tests/test_oid_catalog.py::test_unknown_firmware_raises_catalog_not_found -xvs
```
