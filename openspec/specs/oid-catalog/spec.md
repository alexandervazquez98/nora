# oid-catalog Specification

## Purpose

Defines the air-gapped, cryptographically-pinned OID catalog that maps OIDs to public Cambium PMP 450i object names. The catalog ships as JSON files keyed by vendor, model, and firmware version, is verified at boot via HMAC-SHA256, and MUST NOT perform any network access. The catalog is the single source of truth for which OIDs the SNMP driver resolves; it prevents runtime MIB fetches and keeps the project free of WHISP-SM-MIB text restricted by EULA.

## Requirements

### Requirement: On-Disk Layout

Catalogs MUST live across two roots: a built-in baseline shipped via package data and an operator override directory at `Settings.oid_catalogs_path`. On any `(vendor, model, firmware)` conflict the operator override MUST win.

#### Scenario: operator override wins on conflict

- GIVEN the built-in and operator roots both contain a valid signed `(cambium, pmp450i, 15.2.1)`
- WHEN boot resolves the catalog
- THEN the operator copy is loaded AND the built-in copy is discarded

### Requirement: HMAC-SHA256 Boot Verification

Every catalog file MUST be verified at boot using HMAC-SHA256. The signing key MUST come from `Settings.oid_catalog_signing_key`; the boot sequence reads the key once, verifies all catalog files, and refuses to start on tamper or signature mismatch.

#### Scenario: valid catalog verifies on boot

- GIVEN a catalog file whose HMAC matches `Settings.oid_catalog_signing_key`
- WHEN boot runs
- THEN verification passes and the catalog is loaded

#### Scenario: tampered or missing-key catalog raises a typed exception

- GIVEN a catalog whose bytes no longer match its HMAC, OR an empty/absent key
- WHEN boot runs
- THEN a typed `CatalogVerificationError` is raised
- AND the driver does not start

### Requirement: Key Rotation Behaviour

When the operator changes `oid_catalog_signing_key`, the new key MUST be used on the next boot. Old catalogs MUST NOT be accepted under the new key; any kept catalog MUST be re-signed.

#### Scenario: key rotation invalidates previously-signed catalogs

- GIVEN a catalog signed with `KEY_A` and `Settings.oid_catalog_signing_key == "KEY_B"`
- WHEN boot runs
- THEN a typed `CatalogVerificationError` is raised
- AND a re-signed catalog under `KEY_B` succeeds

### Requirement: Catalog Schema Validation

Every catalog MUST parse as JSON and MUST contain every OID required for its `(vendor, model)` triple. Missing required OIDs MUST be treated as a verification failure. The required-OID set MUST be scoped per `(vendor, model)`, not a single global set. (Previously: `REQUIRED_OIDS` was a module-level `frozenset[str]` shared across all triples; ADR #17 risk mitigation.)

#### Scenario: missing OID fails verification per triple

- GIVEN a `(cambium, pmp450i, 15.2.1)` JSON missing `radioDownlinkRate`
- WHEN boot validates the schema
- THEN a typed `CatalogVerificationError` is raised naming the missing key

### Requirement: No Vendor MIB Text

Catalog files MUST contain only stable public object names as keys plus dotted OID strings. MIB prose, `OBJECT-TYPE`, or `MODULE-IDENTITY` excerpts MUST NOT appear in any catalog file or any file under `src/nora/`.

#### Scenario: catalogs contain only public names + dotted OIDs

- GIVEN every `*.json` file under `data/oid-catalogs/`
- WHEN a content scan looks for vendor-prose markers
- THEN zero matches are found

### Requirement: No Network During Catalog Resolution

The catalog loader MUST NOT import `requests`, `httpx`, `urllib.request`, `socket`, `ssl`, or `http.client`.

#### Scenario: catalog loader is network-free

- GIVEN every module that loads a catalog
- WHEN a static scan runs for the banned imports
- THEN zero matches are found
- AND a runtime test with mocked network symbols confirms none are called

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
## Cross-References

- `secure-configuration` — `oid_catalogs_path` and `oid_catalog_signing_key` follow the additive `Settings` pattern.
- `driver-snmp-pmp450i` — consumes the verified catalog at fetch time.
- `session-journal` — the same AST-air-gap scan pattern is reused.
