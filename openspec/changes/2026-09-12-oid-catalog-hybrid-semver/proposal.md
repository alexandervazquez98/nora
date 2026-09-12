# Proposal: `2026-09-12-oid-catalog-hybrid-semver`

## Intent

`OidCatalogRegistry` resolves one `(vendor, model, firmware)` against one root via exact pin and forbids fuzzy match (`oid_catalog.py:11-13`). That blocks #14 + #15 (multi-vendor, multi-protocol). ADR #17 (issue #17) approves layered load (built-in + signed operator overrides, override wins, fail-fast on signature) and semver resolution (strict-major hard-fail + minor descending fallback + literal LLM warning `"OID catalog fallback: requested X, using Y (minor mismatch)"`). This change closes #13.

## Scope

### In Scope

- Multi-root scan (built-in + operator); override wins on conflict.
- Semver-aware `resolve`: strict-major exact → minor descending → typed failure.
- Literal fallback warning via existing telemetry/sanitizer channel.
- `scripts/sign_catalog.py` parameterised for any `(vendor, model, firmware)`.
- Spec delta for `oid-catalog` + cascading tweaks to `driver-snmp-pmp450i` and `prompt-registry`.

### Out of Scope

- Vendor contribution workflow (P3 DEFERRED per ADR #17 — second vendor required first).
- `Device.vendor/model` Literal widening → #14.
- `prompt-registry` MCP exposure → candidate separate change.
- Exception rename (`NetworkUnreachableError` → generic `TransportError`) → #14.
- `_driver` singleton Protocol rewrite in `drivers/registry.py` → #14.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `oid-catalog` — REMOVE "Runtime fuzzy matching MUST NOT be permitted" + locking test `test_unknown_firmware_raises_catalog_not_found`; ADD multi-root scan, semver resolution, minor-fallback warning scenario.
- `driver-snmp-pmp450i` — MODIFY catalog lookup wording; no driver-boundary change.
- `prompt-registry` — ADD cross-reference to `oid-catalog` (parallel layered-load pattern); no requirement change.

## Approach

Mirror `PromptRegistry.from_settings`: built-in via `importlib.resources`, operator overrides via `Settings.oid_catalogs_path`. Use `packaging.version.Version` for the semver compare (design may pick a custom `FirmwareVersion`). Collect candidates per `(vendor, model)` across roots, run HMAC verify + REQUIRED_OIDS per match, pick best fit. Forecast exceeds 400 LOC; `auto-chain` splits into `p1-hybrid-loading` → `p2-semver-resolution` PRs without re-asking.

## Affected Areas

| Area | Impact | Description |
|------|--------|-------------|
| `src/nora/drivers/oid_catalog.py` | Modified | Multi-root scan + semver resolver |
| `src/nora/data/oid-catalogs/` | New | Packaged built-in baseline |
| `scripts/sign_catalog.py` | Modified | Vendor/model/firmware CLI args |
| `tests/test_oid_catalog.py` | Modified | Replace locking test; add #17 named tests |
| `openspec/specs/{oid-catalog,driver-snmp-pmp450i,prompt-registry}/spec.md` | Modified | Delta at archive time |

## Risks

| Risk | Lik | Mitigation |
|------|-----|------------|
| Semver parser mishandles pre-release / build metadata | Med | Pin `packaging>=24.0`; named test for pre-release exclusion |
| Global `REQUIRED_OIDS` blocks second vendor | Med | Move to per-`(vendor, model)` mapping in this change |
| `Literal["cambium"]` widening breaks openchat | High | Device typing widening is OUT of scope |
| Override vs built-in duplication silent | Low | Override wins on `(vendor, model, major)`; named test asserts |

## Rollback Plan

Revert the merge. `resolve` returns to exact-pin (8 of 10 named tests in #17 still pass against old behaviour). Built-in baseline ships as a new tree; deleting it returns to override-only. No operator-data migration.

## Success Criteria

- [ ] All 10 named tests in ADR #17 pass.
- [ ] `verify_all` accepts ≥2 roots; override wins on conflict.
- [ ] Strict-major mismatch → typed exception; minor mismatch → fallback + literal warning.
- [ ] `Literal["cambium"]` / `Literal["pmp450i"]` on `Device` preserved.
- [ ] Toolchain green (`ruff` + `mypy --strict`) and coverage ≥ 85% (no regression).
- [ ] PR description closes #13; commit body references #17.
