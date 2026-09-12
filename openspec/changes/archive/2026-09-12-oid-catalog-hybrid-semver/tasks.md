# Tasks: 2026-09-12-oid-catalog-hybrid-semver

Auto-chain `stacked-to-main`: PR 1 → main → PR 2 → main. Each PR ≤400 lines; strict TDD forward-compatible with `sdd-apply-minim`.

## PR 1: p1-hybrid-loading

Two-root scan (built-in via `importlib.resources` + operator via `Settings.oid_catalogs_path`, override wins). `resolve()` unchanged; operator-only tests bit-identical.

### WU 1.1 conftest fixtures
- **Type**: test · **Depends on**: —
- **Acceptance**: `sample_catalog(vendor=, model=, firmware=)` kwargs + `tmp_builtin_root` fixture exposed.
- **Files**: `tests/conftest.py` · **Cmd**: `uv run pytest tests/test_oid_catalog.py --co -q | grep sample_catalog`

### WU 1.2 pin packaging>=24.0
- **Type**: impl · **Depends on**: —
- **Acceptance**: `Version("1.2.3-rc.1").base_version == "1.2.3"` importable.
- **Files**: `pyproject.toml` · **Cmd**: `uv run python -c "from packaging.version import Version; assert Version('1.2.3-rc.1').base_version == '1.2.3'"`

### WU 1.3 per-(vendor,model) REQUIRED_OIDS table
- **Type**: impl+test · **Depends on**: 1.1
- **Acceptance**: `_REQUIRED_OIDS_BY_VENDOR_MODEL[(cambium,pmp450i)]` covers 6 OIDs; `REQUIRED_OIDS` derived alias. Scenario `missing OID fails verification per triple` green.
- **Files**: `src/nora/drivers/oid_catalog.py`, `tests/test_oid_catalog.py` · **Cmd**: `uv run pytest tests/test_oid_catalog.py::TestMultiRoot::test_missing_oid_fails_per_triple -xvs`

### WU 1.4 verify(built_in_root, operator_root, signing_key)
- **Type**: impl+test · **Depends on**: 1.3
- **Acceptance**: `verify()` returns registry covering both roots; `verify_all(settings)` thin wrapper. R1–R7 verbatim green.
- **Files**: `src/nora/drivers/oid_catalog.py`, `tests/test_oid_catalog.py` · **Cmd**: `uv run pytest tests/test_oid_catalog.py -k "not TestMultiRoot" -q`

### WU 1.5 deterministic two-root scan + override precedence
- **Type**: impl+test · **Depends on**: 1.4
- **Acceptance**: `sorted(iterdir)` + `sorted(rglob)`; operator overwrites built-in on `(v,m,f)` collision; dup-within-root → `CatalogVerificationError`. Scenarios `operator override wins` + `deterministic two-root scan` green.
- **Files**: `src/nora/drivers/oid_catalog.py`, `tests/test_oid_catalog.py` · **Cmd**: `uv run pytest tests/test_oid_catalog.py::TestMultiRoot -k "operator_wins or deterministic" -xvs`

### WU 1.6 fail-fast on invalid HMAC in any root
- **Type**: impl+test · **Depends on**: 1.4
- **Acceptance**: `_verify_one` reused; tampered file in either root aborts boot. Scenario `invalid HMAC in any root aborts boot` green.
- **Files**: `src/nora/drivers/oid_catalog.py`, `tests/test_oid_catalog.py` · **Cmd**: `uv run pytest tests/test_oid_catalog.py::TestMultiRoot::test_invalid_hmac_in_any_root_raises_catalog_verification_error -xvs`

### WU 1.7 ship built-in baseline via package data
- **Type**: impl+test · **Depends on**: 1.5, 1.8
- **Acceptance**: `data/__init__.py` + `data/oid-catalogs/cambium/pmp450i/15.2.1.json` (signed via WU 1.8) ship; `importlib.resources.files("nora.data.oid_catalogs")` resolves them.
- **Files**: `src/nora/data/__init__.py`, `src/nora/data/oid-catalogs/cambium/pmp450i/15.2.1.json`, `tests/test_oid_catalog.py`, plus `tests/test_sign_catalog.py` (NEW) and signing-key swaps in `tests/test_server.py`, `tests/test_integration.py`, `tests/test_integration_boot.py`, `tests/test_main_alias.py` (BUILTIN_BASELINE_SIGNING_KEY to keep subprocess-boot tests green post-PR1; no semantic change) · **Cmd**: `uv run pytest tests/test_oid_catalog.py::TestMultiRoot::test_builtin_baseline_loads_via_importlib_resources -xvs`

### WU 1.8 parameterise sign_catalog.py
- **Type**: impl+test · **Depends on**: 1.1
- **Acceptance**: `--vendor/--model/--firmware` args with backward-compat defaults `(cambium, pmp450i, 15.2.1)`; sign+verify roundtrip works for arbitrary triple.
- **Files**: `scripts/sign_catalog.py`, `tests/test_sign_catalog.py` · **Cmd**: `uv run pytest tests/test_sign_catalog.py -xvs`

### Verification (PR 1)
- **Cmd**: `uv run pytest -q && uv run ruff check . && uv run mypy --strict src/nora && uv run ruff format --check .`
- **Expected**: 11 existing `test_oid_catalog` + 9 new `TestMultiRoot` + 5 new `TestSignCatalog` = 14 new test methods; full suite 294 pass + 2 skipped (pre-existing timeout); ruff + mypy + format clean.

### Rollback (PR 1)
Revert merge. `verify_all(settings)` returns to single-root; deleting `src/nora/data/oid-catalogs/` removes baseline. No operator data migration; R1–R7 + locking fixture bit-identical against pre-PR1 (operator-only path).

## PR 2: p2-semver-resolution

Rewrites `resolve(ref)`: `Version(...).base_version` compare, strict-major hard fail, minor descending fallback + literal `logger.warning`. Depends on PR 1's `_REQUIRED_OIDS_BY_VENDOR_MODEL`.

### WU 2.1 semver-aware resolve + pre-release strip
- **Type**: impl+test · **Depends on**: PR 1 merged
- **Acceptance**: exact match silent; `15.2.1-rc.1` → `15.2.1` without warning. Scenarios `exact match returns without warning` + `pre-release request matches the bare version` green.
- **Files**: `src/nora/drivers/oid_catalog.py`, `tests/test_oid_catalog.py` · **Cmd**: `uv run pytest tests/test_oid_catalog.py::TestSemverResolution -k "exact_match or pre_release" -xvs`

### WU 2.2 strict-major hard fail
- **Type**: impl+test · **Depends on**: 2.1
- **Acceptance**: `16.0.0` vs `15.x` raises `CatalogNotFoundError(ref)` whose message names both majors. Scenario `major mismatch raises a typed exception` green.
- **Files**: `src/nora/drivers/oid_catalog.py`, `tests/test_oid_catalog.py` · **Cmd**: `uv run pytest tests/test_oid_catalog.py::TestSemverResolution::test_major_mismatch_raises_typed_exception -xvs`

### WU 2.3 minor descending fallback + literal warning
- **Type**: impl+test · **Depends on**: 2.2
- **Acceptance**: `15.3.1` vs `(15.2.1, 15.3.0)` returns `15.3.0`; `caplog.records[0].message == "OID catalog fallback: requested 15.3.1, using 15.3.0 (minor mismatch)"` exact. Scenario `minor mismatch returns closest lower minor with literal warning` green.
- **Files**: `src/nora/drivers/oid_catalog.py`, `tests/test_oid_catalog.py` · **Cmd**: `uv run pytest tests/test_oid_catalog.py::TestSemverResolution::test_minor_mismatch_returns_closest_lower_minor_with_literal_warning -xvs`

### WU 2.4 reissue locking fixture
- **Type**: test · **Depends on**: 2.2
- **Acceptance**: `test_unknown_firmware_raises_catalog_not_found` fixture unchanged (requests `99.0.0` vs `15.x`); assertion updated so message names both majors. Per-Firmware Pin wording removed.
- **Files**: `tests/test_oid_catalog.py` · **Cmd**: `uv run pytest tests/test_oid_catalog.py::test_unknown_firmware_raises_catalog_not_found -xvs`

### Verification (PR 2)
- **Cmd**: `uv run pytest -q && uv run ruff check . && uv run mypy --strict src/nora && uv run ruff format --check .`
- **Expected**: 13 post-PR1 + 4 new `TestSemverResolution` = 17 pass; `caplog.records[0].message` exact-match; toolchain green.

### Rollback (PR 2)
Revert merge. `resolve()` returns to exact-pin; pre-release strip + minor-fallback + `logger.warning` removed. PR 1's two-root load preserved.

## Review Workload Forecast

| Field | Value |
|-------|-------|
| PR 1 changed lines | ~270 |
| PR 2 changed lines | ~160 |
| Total changed lines | ~430 |
| 400-line budget risk per PR | Low (PR 1: 130 headroom; PR 2: 240) |
| Chained PRs recommended | Yes (auto-chain per proposal) |

Decision needed before apply: No
Chained PRs recommended: Yes
Chain strategy: stacked-to-main
400-line budget risk: Low

**Rationale**: PR 1 stays under 300 because `verify()` delegates to existing `_verify_one` verbatim (design #7) and per-`(vendor,model)` table replaces global set with minimal churn. PR 2 stays under 200 because `resolve()` is rewritten in-place — `Version(...).base_version` strips pre-release/build in one expression, `logger.warning` is one call. Both land within budget; auto-chain decided in proposal without re-asking.

## Dependency Graph

```
PR 1: 1.1 → 1.3 → 1.4 → {1.5 → 1.7, 1.6}
       1.2 (parallel)         1.8 → 1.7
       ▼ merge PR 1 → main (13 tests pass)
PR 2: 2.1 → 2.2 → {2.3, 2.4}
       ▼ merge PR 2 → main (17 tests pass)
```

Zero WUs run parallel across PRs. PR 2 starts only after PR 1 green on main.