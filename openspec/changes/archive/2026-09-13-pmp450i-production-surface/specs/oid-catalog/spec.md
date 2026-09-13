# Delta for oid-catalog

> Modified by `2026-09-13-pmp450i-production-surface` — `REQUIRED_OIDS` becomes per-`(vendor, model)` AND per-tool; the migration tool resolves TWO triples (current + candidate); the minor-mismatch warning emission requirement extends from "driver path" to "any tool path that calls the catalog".

## MODIFIED Requirements

### Requirement: Catalog Schema Validation

Every catalog MUST parse as JSON and MUST contain every OID required for its `(vendor, model)` triple AND every tool name registered for that triple. The required-OID set MUST be scoped per `(vendor, model)`, not a single global set; the per-tool index is sourced from the envelope's `"tools"` map. Missing required OIDs or missing per-tool index entries MUST be treated as a verification failure. (Previously: `REQUIRED_OIDS` was a module-level `frozenset[str]` shared across all triples; ADR #17 risk mitigation. Now: per-`(vendor, model)` AND per-tool, scoped via the envelope.)

#### Scenario: missing OID fails verification per triple

- GIVEN a `(cambium, pmp450i, 15.2.1)` JSON missing `radioDownlinkRate`
- WHEN boot validates the schema
- THEN a typed `CatalogVerificationError` is raised naming the missing key

#### Scenario: missing per-tool index entry fails verification

- GIVEN the envelope omits `"tools": {"snmp_get_ap_summary": [...]}` for `(cambium, pmp450i, 15.2.1)`
- WHEN boot validates the schema
- THEN a typed `CatalogVerificationError` is raised naming the missing tool name

### Requirement: Minor Descending Fallback With Literal Warning

When the requested firmware major matches but the exact minor is absent, `resolve` MUST return the highest registered minor strictly less than the request (deterministic across runs) AND emit the literal string `"OID catalog fallback: requested X, using Y (minor mismatch)"` (X = requested, Y = chosen) through the existing telemetry/sanitizer channel. This requirement extends from the driver path to ANY path that calls `resolve` — including the new `@mcp.tool`s from `pmp450i-radio-tools` and the `report_firmware → catalog resolve` E2E path. (Previously: the warning was scoped to the driver path; ADR #17 Test 9 + 10 demand the same surface for every tool caller.)

#### Scenario: minor mismatch returns closest lower minor with literal warning via the tool path

- GIVEN the registry holds `(cambium, pmp450i, 15.2.1)` and `(cambium, pmp450i, 15.3.0)`
- WHEN `snmp_get_ap_summary(device_id)` runs end-to-end (tool calls `resolve`)
- THEN the catalog at `15.3.0` is used AND the literal `"OID catalog fallback: requested 15.3.1, using 15.3.0 (minor mismatch)"` is emitted through the telemetry channel

## ADDED Requirements

### Requirement: Migration Resolves Two Triples (Current + Candidate)

`OidCatalogRegistry` MUST expose `resolve_migration_refs(current_ref, candidate_ref) -> tuple[OidCatalog, OidCatalog]` so `snmp_migrate_radio_frequency` validates BOTH the source and the destination catalogs BEFORE any SNMP SET frame is sent. If either resolve raises, the tool MUST abort and no SET frame SHALL be emitted. (ADR #17 Test 10 + issue #15 cluster 3.)

#### Scenario: both triples resolve before the wire frame

- GIVEN `(cambium, pmp450i, 15.2.1)` and `(cambium, pmp450i, 15.2.1)` are registered (same minor, candidate carrier frequency may differ)
- WHEN `resolve_migration_refs((cambium, pmp450i, 15.2.1), (cambium, pmp450i, 15.2.1))` runs
- THEN both catalogs are returned AND no SNMP SET is sent by `resolve_migration_refs`

#### Scenario: candidate major mismatch aborts the migration pre-wire

- GIVEN the registry holds only `(cambium, pmp450i, 15.x)` AND the candidate carrier requires `(cambium, pmp450i, 16.x)` semantics
- WHEN `resolve_migration_refs(...)` runs
- THEN `CatalogNotFoundError` is raised AND the migration tool aborts without sending SET frames

### Requirement: Per-Tool Index Is Stable Across Runs

The per-tool index MUST be deterministic regardless of catalog filesystem order. The index is computed once at boot from a single sorted pass over the envelope's `"tools"` map; subsequent `resolve` and `resolve_migration_refs` calls reuse it without re-walking disk.

#### Scenario: index is identical across two boots with identical inputs

- GIVEN the same catalogs and signing key
- WHEN `verify_all` runs twice
- THEN the per-tool index entries are identical AND no entry is added or dropped between boots