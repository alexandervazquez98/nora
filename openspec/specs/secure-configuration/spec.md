# secure-configuration Specification

## Purpose

Defines how NORA loads runtime configuration through Pydantic `Settings`, never reads `os.environ` inline, ships a public sanitized `.env.example`, fails fast on missing required values, and isolates per-provider credentials so only the active provider is consulted. The capability is the gatekeeper for SCOPE §2 Zero-Leakage Policy at the application boundary: no credential or infrastructure value is ever embedded in code or visible in tool output.

## Requirements

### Requirement: Pydantic Settings Is the Only Configuration Source

All runtime configuration MUST be loaded via a single Pydantic `Settings` subclass; code under `src/nora/` MUST NOT call `os.environ` directly.

#### Scenario: settings load from `.env`

- GIVEN `.env` sets `NORA_LLM_PROVIDER=lmstudio`
- WHEN `Settings()` is instantiated
- THEN the `provider` field equals `lmstudio`
- AND no `os.environ` call appears anywhere in `src/nora/`

#### Scenario: missing required setting fails fast

- GIVEN `.env` is absent and the active provider requires `GEMINI_API_KEY`
- WHEN the process starts
- THEN startup aborts with a typed validation error naming the missing field
- AND the exit code is non-zero

#### Scenario: extra unknown settings are ignored

- GIVEN `.env` contains `NORA_UNKNOWN_KEY=foo`
- WHEN `Settings()` is instantiated
- THEN the extra key is ignored and no error is raised

### Requirement: Synthetic `.env.example`

A tracked `.env.example` MUST list every variable the application reads, MUST use clearly synthetic placeholder values, and MUST NOT contain any real credential, IP, hostname, MAC, or serial number.

#### Scenario: `.env.example` contains placeholders only

- GIVEN `.env.example` is committed
- WHEN a reviewer greps it for `=` values
- THEN every value is a placeholder such as `change-me` or an RFC 5737 documentation IP
- AND no value matches a private IPv4 pattern

#### Scenario: `.env.example` mirrors every read variable

- GIVEN a new field is added to `Settings`
- WHEN the implementation lands
- THEN the same key appears in `.env.example`
- AND a checklist or test flags a mismatch

### Requirement: Provider Credential Isolation

Only the active provider's credential MUST be required at startup; inactive providers' credentials MUST NOT be loaded, logged, or transmitted.

#### Scenario: `lmstudio` active does not require `GEMINI_API_KEY`

- GIVEN `NORA_LLM_PROVIDER=lmstudio` and no `GEMINI_API_KEY` in `.env`
- WHEN the process starts
- THEN startup succeeds
- AND no Gemini credential is read or echoed

#### Scenario: `gemini` active requires `GEMINI_API_KEY`

- GIVEN `NORA_LLM_PROVIDER=gemini` and no `GEMINI_API_KEY` in `.env`
- WHEN the process starts
- THEN startup aborts with a validation error naming `GEMINI_API_KEY`

### Requirement: Credentials Never Appear in String Representations

`repr()` and `str()` of any `Settings` instance, log line, or MCP tool response MUST NOT contain the value of any secret field.

#### Scenario: `repr(settings)` masks secrets

- GIVEN `Settings()` with `GEMINI_API_KEY=secret-value`
- WHEN `repr(settings)` is evaluated
- THEN the literal `secret-value` is NOT present
- AND the masked form is shown instead

#### Scenario: log lines never contain secrets

- GIVEN the application logs `Settings loaded`
- WHEN a developer inspects the log
- THEN no line contains the value of any secret field

#### Scenario: tool responses never contain secrets

- GIVEN `nora_health` is invoked
- WHEN the tool response is serialised
- THEN no credential value appears in any field

### Requirement: Settings Load Status Is Observable

`Settings` MUST expose a `loaded_from` field; `nora_health` MUST surface it.

#### Scenario: `.env` precedence over process environment

- GIVEN `.env` sets `NORA_LLM_PROVIDER=gemini`
- AND the process env exports `NORA_LLM_PROVIDER=lmstudio`
- WHEN `Settings()` is instantiated
- THEN `provider == "gemini"` (`.env` wins)
- AND `loaded_from` lists `.env`

#### Scenario: defaults are explicit when nothing is set

- GIVEN no `.env` and no relevant env vars are present
- WHEN `Settings()` is instantiated
- THEN every field has the documented default
- AND `loaded_from` lists `defaults`

### Requirement: Telemetry Path Sanitization Boundary

Before any user-supplied value leaves the process (LLM request, MCP tool payload), the telemetry sanitizer MUST be invoked; configuration values MUST never be passed to the sanitizer as user content.

#### Scenario: settings values flow untouched to providers

- GIVEN `Settings.model_id == "some-model"`
- WHEN the LLM provider client is built
- THEN `model_id` is passed verbatim to the SDK
- AND the sanitizer is NOT applied to `model_id`

#### Scenario: user-supplied prompt is sanitized before send

- GIVEN a user prompt contains a private IPv4 literal
- WHEN the LLM provider receives the prompt
- THEN the literal has been replaced by a synthetic alias
- AND the SDK never observes the original literal

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
