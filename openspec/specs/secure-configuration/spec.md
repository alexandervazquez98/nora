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

## Cross-References

- Tool-response secret redaction: `nora-mcp-server > Security Boundary — No Secrets in Tool Responses`
