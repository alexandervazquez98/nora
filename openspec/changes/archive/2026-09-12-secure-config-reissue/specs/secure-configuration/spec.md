# Delta for secure-configuration

## ADDED Requirements

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