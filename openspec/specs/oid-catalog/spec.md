# oid-catalog Specification

## Purpose

Defines the air-gapped, cryptographically-pinned OID catalog that maps OIDs to public Cambium PMP 450i object names. The catalog ships as JSON files keyed by vendor, model, and firmware version, is verified at boot via HMAC-SHA256, and MUST NOT perform any network access. The catalog is the single source of truth for which OIDs the SNMP driver resolves; it prevents runtime MIB fetches and keeps the project free of WHISP-SM-MIB text restricted by EULA.

## Requirements

### Requirement: On-Disk Layout

Catalogs MUST live under `<oid_catalogs_path>/{vendor}/{model}/{firmware}.json`. Each catalog is one JSON object whose keys are stable object names (e.g. `radioDownlinkRate`) and values are dotted OID strings. Layout is per-firmware pinned.

#### Scenario: catalog file is at the pinned path

- GIVEN a `Device` with `vendor == "cambium"`, `model == "pmp450i"`, `firmware == "15.2.1"`
- WHEN boot resolves the catalog
- THEN the file at `<oid_catalogs_path>/cambium/pmp450i/15.2.1.json` is read

### Requirement: Per-Firmware Pin

The driver MUST pin a catalog per `firmware` version. Runtime fuzzy matching MUST NOT be permitted; an unknown pin fails closed.

#### Scenario: unknown firmware pin is rejected

- GIVEN a `Device` with `firmware == "99.0.0"` and no matching catalog file
- WHEN boot resolves the catalog
- THEN a typed `CatalogNotFoundError` is raised
- AND the driver does not start

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

Every catalog file MUST parse as JSON and MUST contain every OID required to build a `RadioMetricsReport`. Missing required OIDs MUST be treated as a verification failure.

#### Scenario: catalog missing required OID fails validation

- GIVEN a JSON file missing the `radioDownlinkRate` key
- WHEN boot validates the schema
- THEN a typed `CatalogVerificationError` is raised
- AND the failure lists the missing key

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

## Cross-References

- `secure-configuration` — `oid_catalogs_path` and `oid_catalog_signing_key` follow the additive `Settings` pattern.
- `driver-snmp-pmp450i` — consumes the verified catalog at fetch time.
- `session-journal` — the same AST-air-gap scan pattern is reused.
