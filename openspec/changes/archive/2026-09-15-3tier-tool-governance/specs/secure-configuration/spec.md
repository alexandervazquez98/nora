# Delta for secure-configuration

## Reconciliation

**MODIFIED** under `openspec/changes/2026-09-15-3tier-tool-governance/`.
Base spec at `openspec/specs/secure-configuration/spec.md`. Purely
additive — R1..R6 preserved. Two new Settings fields land for the HMAC
signing key and the operator-overridable tool-spec directory.

## ADDED Requirements

### Requirement: `nora_hitl_signing_key` Setting

`Settings` SHALL expose a new field
`nora_hitl_signing_key: SecretStr` mirroring the existing
`nora_oid_catalog_signing_key` pattern. The field MUST be sourced from
the `NORA_HITL_SIGNING_KEY` env var / `.env` key. Boot MUST fail
closed with a typed validation error if a Tier-2 tool is invoked while
this key is empty (mirrors the catalog key fail-closed pattern).

(Previously: no HITL signing-key setting existed; the stub verifier
needed no secret.)

#### Scenario: Settings exposes nora_hitl_signing_key as SecretStr

- GIVEN `.env` sets `NORA_HITL_SIGNING_KEY=test-only-key-do-not-use`
- WHEN `Settings()` is instantiated
- THEN `settings.nora_hitl_signing_key.get_secret_value() ==
  "test-only-key-do-not-use"` AND `repr(settings)` masks the value

#### Scenario: missing HITL signing key fails closed on Tier-2 invocation

- GIVEN `Settings.nora_hitl_signing_key` is empty
- WHEN `snmp_migrate_radio_frequency(...)` is invoked (a Tier-2 tool)
- THEN a typed error is raised AND the literal
  `AutonomousMutationRejected` message is preserved (no regression
  to the `pmp450i-radio-tools` literal-message contract)

#### Scenario: Tier-0 / Tier-1 tools do not require the HITL signing key

- GIVEN `Settings.nora_hitl_signing_key` is empty
- WHEN a Tier-0 tool (`snmp_get_ap_summary(...)`) or a Tier-1 tool
  (`snmp_run_spectrum_analysis(...)` with `operator_confirmed=True`)
  is invoked
- THEN no signing-key error is raised AND the tool proceeds (the key
  is required only for Tier-2 tools)

### Requirement: `nora_tool_specs_dir` Setting

`Settings` SHALL expose a new field `nora_tool_specs_dir: Path | None`
(default `Path("docs/tool_specs")`, resolved relative to the repo
root, or `None` if the path does not exist on the filesystem). When
the path resolves and the directory exists,
`PromptRegistry.from_settings` SHALL scan it; when it does not exist,
the scan is skipped (no fatal).

(Previously: no tool-spec-dir setting existed; the registry scanned
only one source dir.)

#### Scenario: default tool-spec dir is resolved against the repo root

- GIVEN `.env` does NOT set `NORA_TOOL_SPECS_DIR` AND
  `docs/tool_specs/` exists relative to the repo root
- WHEN `Settings()` is instantiated
- THEN `settings.nora_tool_specs_dir == Path("docs/tool_specs")` (or
  absolute-equivalent) AND `Path(settings.nora_tool_specs_dir).is_dir()`

#### Scenario: operator override wins over default

- GIVEN `.env` sets `NORA_TOOL_SPECS_DIR=/etc/nora/tool_specs/`
- WHEN `Settings()` is instantiated
- THEN `settings.nora_tool_specs_dir == Path("/etc/nora/tool_specs/")`
  AND the override takes precedence

#### Scenario: missing directory is non-fatal

- GIVEN `.env` sets `NORA_TOOL_SPECS_DIR=/nonexistent/path`
- WHEN `PromptRegistry.from_settings(settings)` runs
- THEN `nora_tool_specs_dir` resolves to `None` AND the scan skips
  it AND `from_settings` does not raise

### Requirement: `.env.example` Mirrors New Fields

The committed `.env.example` SHALL list `NORA_HITL_SIGNING_KEY` AND
`NORA_TOOL_SPECS_DIR` with synthetic placeholder values, SHALL NOT
exceed the documented line cap (`< 30` lines after this change, vs the
prior `< 20`), and SHALL NOT list any of the nine former LLM or
journal keys.

#### Scenario: .env.example lists every new field

- GIVEN `.env.example` is committed
- WHEN the file is grep'd for the two new field names
- THEN `NORA_HITL_SIGNING_KEY` AND `NORA_TOOL_SPECS_DIR` both appear
  AND each value is a placeholder (`change-me` or RFC 5737 IP)

#### Scenario: .env.example line cap is respected

- GIVEN the surviving seven fields PLUS the two new fields = nine
  total
- WHEN the file's line count is checked
- THEN it is `< 30` lines (relaxed from the prior `< 20` cap)

## Open Questions (deferred to design)

- **Q5 (signing-key rotation):** how operators rotate
  `nora_hitl_signing_key` without invalidating in-flight tokens.
  This change ships the single-key contract; rotation story is a
  follow-up.
- **Q6 (coverage threshold):** the new Settings fields increase the
  covered-module denominator. Apply phase must hold `coverage_threshold: 85`.

## Cross-References

`hitl-approval-tokens` (consumer of `nora_hitl_signing_key`),
`prompt-registry` (consumer of `nora_tool_specs_dir`),
`nora-mcp-server` (boot reads the two new fields), `pmp450i-radio-tools`
(fail-closed on missing key asserted for Tier-2 tools).