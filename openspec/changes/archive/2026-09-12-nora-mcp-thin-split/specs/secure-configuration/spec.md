# Delta for secure-configuration

## REMOVED Requirements

### Requirement: Provider Credential Isolation

(Reason: Thin MCP has no LLM provider; five LLM fields eliminated — `explore.md` L141-153.)
(Migration: None. `_check_provider_credential` deleted from `src/nora/config.py:167-172`; `ProviderName`, `_DEFAULT_OPERATOR_ALIAS`, `LoadSource` lose all callers.)

### Requirement: Telemetry Path Sanitization Boundary

(Reason: Scenarios referenced `provider.complete(...)`; no provider exists.)
(Migration: None. `telemetry-sanitizer` remains load-bearing — see `nora-mcp-server/spec.md`.)

### Requirement: Credentials Never Appear in String Representations

(Reason: Examples used `GEMINI_API_KEY`; re-issued under MODIFIED with `nora_oid_catalog_signing_key`.)

### Requirement: Settings Load Status Is Observable

(Reason: Examples used `NORA_LLM_PROVIDER` and `nora_health`; re-issued under MODIFIED.)

## MODIFIED Requirements

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

### Requirement: Credentials Never Appear in String Representations

`repr()` and `str()` of any `Settings` instance, log line, or MCP tool response MUST NOT contain the value of any secret field.

#### Scenario: `repr(settings)` masks secrets

- GIVEN `Settings()` with `nora_oid_catalog_signing_key == "change-me"`
- WHEN `repr(settings)` is evaluated
- THEN the literal `change-me` is NOT present

#### Scenario: log lines never contain secrets

- GIVEN the application logs `Settings loaded`
- WHEN a developer inspects the log
- THEN no line contains the value of any secret field

#### Scenario: tool responses never contain secrets

- GIVEN `Settings.nora_oid_catalog_signing_key` is set
- WHEN any of the four MCP tools is invoked
- THEN no response field contains the signing key value

### Requirement: Settings Load Status Is Observable

`Settings` MUST expose a `loaded_from` field. The `settings_customise_sources` precedence swap (`.env` > process env > defaults) MUST continue to apply to every surviving field.

#### Scenario: `.env` precedence over process environment

- GIVEN `.env` sets `NORA_INTERVENTIONS_DIR=./data/int/`
- AND the process env exports `NORA_INTERVENTIONS_DIR=/elsewhere/int/`
- WHEN `Settings()` is instantiated
- THEN `nora_interventions_dir == "./data/int/"` (`.env` wins) AND `loaded_from` lists `.env`

#### Scenario: defaults are explicit when nothing is set

- GIVEN no `.env` and no relevant env vars are present
- WHEN `Settings()` is instantiated
- THEN every surviving field has the documented default AND `loaded_from` lists `defaults`

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

## ADDED Requirements

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
