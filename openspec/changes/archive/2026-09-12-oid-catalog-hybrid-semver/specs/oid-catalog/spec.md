# Spec Delta: 2026-09-12-oid-catalog-hybrid-semver

Materialises ADR #17 (issue #17): multi-root scan (built-in + operator override, override wins) and semver-aware firmware resolution (strict-major hard-fail, minor descending fallback, literal LLM warning).

## MODIFIED Requirements

### Requirement: On-Disk Layout

Catalogs MUST live across two roots: a built-in baseline shipped via package data and an operator override directory at `Settings.oid_catalogs_path`. On any `(vendor, model, firmware)` conflict the operator override MUST win.

#### Scenario: operator override wins on conflict

- GIVEN the built-in and operator roots both contain a valid signed `(cambium, pmp450i, 15.2.1)`
- WHEN boot resolves the catalog
- THEN the operator copy is loaded AND the built-in copy is discarded

### Requirement: Catalog Schema Validation

Every catalog MUST parse as JSON and MUST contain every OID required for its `(vendor, model)` triple. Missing required OIDs MUST be treated as a verification failure. The required-OID set MUST be scoped per `(vendor, model)`, not a single global set. (Previously: `REQUIRED_OIDS` was a module-level `frozenset[str]` shared across all triples; ADR #17 risk mitigation.)

#### Scenario: missing OID fails verification per triple

- GIVEN a `(cambium, pmp450i, 15.2.1)` JSON missing `radioDownlinkRate`
- WHEN boot validates the schema
- THEN a typed `CatalogVerificationError` is raised naming the missing key

## REMOVED Requirements

### Requirement: Per-Firmware Pin

(Reason: Exact-pin-only blocks #14/#15 multi-vendor. Replaced by `Semver-Aware Firmware Resolution` + `Strict-Major Hard Fail`. ADR #17 P2.)
(Migration: Test `test_unknown_firmware_raises_catalog_not_found` is reissued under `Strict-Major Hard Fail` — the fixture requests `99.0.0` against only `15.x` catalogs and still raises a typed `CatalogNotFoundError`.)

## ADDED Requirements

### Requirement: Multi-Root Scan Determinism

`OidCatalogRegistry.verify_all` MUST accept both roots, HMAC-verify every file, and produce a deterministic registry regardless of scan order or filesystem race. Invalid HMAC in any root MUST fail boot fast (no driver start). ADR #17 P1 acceptance: "Override gana sobre built-in", "Catálogo sin firma válida = fail-fast", "Precedencia determinista sin race conditions".

#### Scenario: deterministic two-root scan with override precedence

- GIVEN both roots present with disjoint triples AND one shared `(cambium, pmp450i, 15.2.1)`
- WHEN `verify_all` runs twice with identical inputs
- THEN both runs yield identical `loaded_refs` AND the shared triple resolves to the operator copy

#### Scenario: invalid HMAC in any root aborts boot

- GIVEN the built-in is valid AND the operator root contains one catalog with an invalid HMAC
- WHEN boot runs
- THEN a typed `CatalogVerificationError` is raised AND the driver does not start

### Requirement: Semver-Aware Firmware Resolution

`OidCatalogRegistry.resolve` MUST compare requested vs registered firmware using semver-aware ordering. Pre-release and build metadata MUST be ignored. ADR #17 P2 acceptance: "Firmware idéntico al catálogo: sin warning".

#### Scenario: exact match returns without warning

- GIVEN the registry holds `(cambium, pmp450i, 15.2.1)`
- WHEN `resolve((cambium, pmp450i, 15.2.1))` runs
- THEN the exact catalog is returned AND no warning is emitted

#### Scenario: pre-release request matches the bare version

- GIVEN the registry holds `(cambium, pmp450i, 15.2.1)`
- WHEN `resolve((cambium, pmp450i, 15.2.1-rc.1))` runs
- THEN the catalog at `15.2.1` is returned AND no warning is emitted

### Requirement: Strict-Major Hard Fail

When the requested firmware major differs from every registered catalog for the same `(vendor, model)`, `resolve` MUST raise a typed exception whose message names both majors (requested vs registered). ADR #17 P2 acceptance: "Major mismatch: hard fail con excepción tipada nombrando el major mismatch".

#### Scenario: major mismatch raises a typed exception

- GIVEN the registry holds only `(cambium, pmp450i, 15.2.1)`
- WHEN `resolve((cambium, pmp450i, 16.0.0))` runs
- THEN a typed `CatalogNotFoundError` is raised AND the message mentions both major versions

### Requirement: Minor Descending Fallback With Literal Warning

When the requested firmware major matches but the exact minor is absent, `resolve` MUST return the highest registered minor strictly less than the request (deterministic across runs) AND emit the literal string `"OID catalog fallback: requested X, using Y (minor mismatch)"` (X = requested, Y = chosen) through the existing telemetry/sanitizer channel. ADR #17 P2 acceptance: "Minor mismatch ... fallback al más cercano + warning literal".

#### Scenario: minor mismatch returns closest lower minor with literal warning

- GIVEN the registry holds `(cambium, pmp450i, 15.2.1)` and `(cambium, pmp450i, 15.3.0)`
- WHEN `resolve((cambium, pmp450i, 15.3.1))` runs
- THEN the catalog at `15.3.0` is returned AND the literal `"OID catalog fallback: requested 15.3.1, using 15.3.0 (minor mismatch)"` is emitted through the telemetry channel
