# secure-configuration Specification

## Purpose

Defines how NORA loads runtime configuration through Pydantic `Settings`, never reads `os.environ` inline, ships a public sanitized `.env.example`, fails fast on missing required values, and isolates per-provider credentials so only the active provider is consulted. The capability is the gatekeeper for SCOPE §2 Zero-Leakage Policy at the application boundary: no credential or infrastructure value is ever embedded in code or visible in tool output.

## Requirements

### Requirement: Pydantic Settings Is the Only Configuration Source

All runtime configuration MUST be loaded via a single Pydantic `Settings` subclass; code under `src/nora/` MUST NOT call `os.environ` directly. The class MUST expose exactly seven user-settable fields; the nine former fields MUST NOT be present.

#### Scenario: settings load from `.env`

- GIVEN `.env` sets `NORA_OID_CATALOGS_PATH=./data/oid-catalogs/`
- WHEN `Settings()` is instantiated
- THEN `nora_oid_catalogs_path == "./data/oid-catalogs/"` AND no `os.environ` call appears anywhere in `src/nora/`

#### Scenario: missing required setting fails fast

- GIVEN `.env` is absent and `nora_oid_catalog_signing_key` has no value
- WHEN the process starts
- THEN startup aborts with a typed validation error naming `nora_oid_catalog_signing_key` AND the exit code is non-zero

#### Scenario: extra unknown settings are ignored

- GIVEN `.env` contains `NORA_UNKNOWN_KEY=foo`
- WHEN `Settings()` is instantiated
- THEN the extra key is ignored and no error is raised

### Requirement: Synthetic `.env.example`

A tracked `.env.example` MUST list every variable the application reads (the seven surviving fields), MUST use synthetic placeholder values, MUST NOT exceed 20 lines, and MUST NOT list any of the nine former LLM or journal keys.

#### Scenario: `.env.example` contains placeholders only

- GIVEN `.env.example` is committed
- WHEN a reviewer greps it for `=` values
- THEN every value is a placeholder such as `change-me` or an RFC 5737 documentation IP AND no value matches a private IPv4 pattern

#### Scenario: `.env.example` mirrors every read variable

- GIVEN the seven Settings fields
- WHEN the file is inspected
- THEN each field appears exactly once AND the file is at most 20 lines long

### Requirement: `.env` Is Never Tracked

`.env` MUST be in `.gitignore`; a pre-commit guard MUST reject any attempt to stage it.

#### Scenario: `.env` is excluded from the index

- GIVEN `.gitignore` lists `.env`
- WHEN `git add -A` runs after `.env` is created
- THEN `.env` is not staged

#### Scenario: accidental staging is rejected

- GIVEN a developer runs `git add .env --force`
- WHEN the pre-commit hook runs
- THEN the commit is rejected with a clear message

### Requirement: Operator Can Boot With Only The Seven Surviving Env Vars

`nora-mcp` MUST start successfully when `Settings()` is instantiated with only the seven surviving fields present in the environment; it MUST NOT require any of the nine former fields. `pyproject.toml` MUST NOT list `lmstudio` or `google-genai` in `[project.dependencies]`.

#### Scenario: pyproject.toml has no LLM SDK dependencies

- GIVEN `pyproject.toml`
- WHEN its `[project.dependencies]` block is parsed
- THEN `lmstudio` and `google-genai` are NOT listed

#### Scenario: nora-mcp boots with only the surviving env vars

- GIVEN only `NORA_OID_CATALOGS_PATH`, `NORA_DEVICES_INVENTORY_PATH`, `NORA_OID_CATALOG_SIGNING_KEY`, `NORA_INTERVENTIONS_DIR`, `NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS`, `NORA_INTERVENTIONS_CORRELATE_SCAN_LIMIT` are exported
- WHEN `nora-mcp` is invoked as a subprocess
- THEN the process starts without error AND the four-tool surface is reachable over stdio
### Requirement: Credentials Never Appear in String Representations

`repr()`, `str()`, or any log line emitted by `Settings` MUST NOT contain the value of any secret field. This contract is scoped to in-process string representations and log output; MCP tool response redaction is owned by `nora-mcp-server > Security Boundary — No Secrets in Tool Responses` and is intentionally NOT re-stated here.

#### Scenario: `repr(settings)` masks secrets

- GIVEN `Settings()` is instantiated with `nora_oid_catalog_signing_key` set to `do-not-leak-this-key`
- WHEN `repr(settings)` is evaluated
- THEN the literal `do-not-leak-this-key` is NOT present AND the masked `SecretStr` form (`**********`) IS present

#### Scenario: log lines never contain secrets

- GIVEN `Settings()` is loaded with `nora_oid_catalog_signing_key` set to `hidden-secret-value`
- WHEN the application logs the `Settings` object via `%r` formatting
- THEN the literal `hidden-secret-value` is NOT present in any captured log line

### Requirement: Settings Load Status Is Observable

`Settings` MUST expose a `loaded_from` field whose value is one of the literals `.env`, `process_env`, or `defaults`. The custom-source precedence (`.env` > process env > defaults) MUST apply to every surviving user-settable field.

#### Scenario: `.env` precedence over process environment

- GIVEN `.env` sets `NORA_OID_CATALOGS_PATH=./data/from-dotenv/`
- AND the process environment exports `NORA_OID_CATALOGS_PATH=./data/from-process-env/`
- WHEN `Settings()` is instantiated
- THEN `nora_oid_catalogs_path == "./data/from-dotenv/"` AND `loaded_from == ".env"`

#### Scenario: process_env branch when no `.env`

- GIVEN no `.env` file is present
- AND the process environment exports `NORA_OID_CATALOGS_PATH=/etc/nora/from-process-env/`
- WHEN `Settings()` is instantiated
- THEN `nora_oid_catalogs_path == "/etc/nora/from-process-env/"` AND `loaded_from == "process_env"`

#### Scenario: defaults are explicit when nothing is set

- GIVEN no `.env` file is present
- AND no `NORA_*`, `LMSTUDIO_*`, or `GEMINI_*` env vars are set
- WHEN `Settings()` is instantiated
- THEN every surviving field has the documented default AND `loaded_from == "defaults"`

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
## Cross-References

- Tool-response secret redaction: `nora-mcp-server > Security Boundary — No Secrets in Tool Responses`
