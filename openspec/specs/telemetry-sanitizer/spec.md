# telemetry-sanitizer Specification

## Purpose

Defines the fixed-policy mask NORA applies to every value about to leave the process — LLM requests, MCP tool payloads, externally-visible log lines. The sanitizer MUST replace private IPv4, MAC, serial numbers, and hostnames with deterministic synthetic aliases so identity-bearing topology never crosses the boundary.

## Requirements

### Requirement: Fixed Mask Categories

The sanitizer MUST mask exactly four identifier categories and nothing else: private IPv4 in `10/8`, `172.16/12`, `192.168/16`, `127/8`; MAC addresses; serial numbers; hostnames. No per-call override.

#### Scenario: private IPv4 is replaced

- GIVEN input contains `10.0.0.5`
- WHEN the sanitizer runs
- THEN the output contains an alias
- AND the original literal is gone

#### Scenario: public IPv4 is not masked

- GIVEN input contains `8.8.8.8`
- WHEN the sanitizer runs
- THEN the output still contains `8.8.8.8`

#### Scenario: MAC, serial, and hostname are replaced

- GIVEN input contains one of each category
- WHEN the sanitizer runs
- THEN each is replaced by an alias
- AND no original literal survives

### Requirement: Deterministic Alias Mapping Within a Session

Within a single sanitizer instance, the same input MUST map to the same alias; distinct inputs MUST map to distinct aliases.

#### Scenario: same input yields same alias across calls

- GIVEN `10.0.0.5` is masked once and returns `RADIO_NODE_A`
- WHEN `10.0.0.5` appears again
- THEN the same alias is used

#### Scenario: distinct inputs yield distinct aliases

- GIVEN `10.0.0.5` and `10.0.0.6` are both masked
- THEN two different aliases are used

### Requirement: Runs Before Any External Call

The sanitizer MUST be invoked on any value leaving the process as part of an LLM request, MCP tool payload, or externally-visible log line; no provider code path may skip it.

#### Scenario: LLM prompt is sanitized before send

- GIVEN a prompt contains a private IPv4 literal
- WHEN the LLM provider receives the prompt
- THEN the prompt has been transformed
- AND the SDK never sees the original literal

#### Scenario: MCP tool output is sanitized before return

- GIVEN a tool result contains a private IPv4 literal
- WHEN the tool returns
- THEN the literal has been replaced by an alias

### Requirement: Pure Function, No I/O

The sanitizer MUST be a pure function: no filesystem, network, environment, or clock access.

#### Scenario: same input yields the same output across calls

- GIVEN the same input text and the same sanitizer instance
- WHEN the sanitizer runs twice
- THEN both outputs are byte-identical

#### Scenario: no I/O is performed

- GIVEN a test mocks `open`, `socket`, and `urllib`
- WHEN the sanitizer runs
- THEN none of those mocks are called

### Requirement: Edge Cases Pass Through Safely

Empty input, no-identifier input, and already-aliased input MUST pass through without raising or mangling.

#### Scenario: empty input returns empty output

- GIVEN the input is `""`
- WHEN the sanitizer runs
- THEN the output is `""` and no exception is raised

#### Scenario: no-identifier or already-aliased input is unchanged

- GIVEN the input contains either no identifier or only `RADIO_NODE_A` and `SWITCH_ACC_01`
- WHEN the sanitizer runs
- THEN the output equals the input byte-for-byte

### Requirement: Failure Surfaces a Typed Error

Non-string input MUST raise a typed exception; callers MUST be able to catch it without aborting the host process.

#### Scenario: `None` or bytes input is rejected

- GIVEN the input is `None` or bytes
- WHEN the sanitizer runs
- THEN a `SanitizerInputError` is raised and no coercion happens

### Requirement: Security Boundary — Credentials Are Out of Scope

The sanitizer does NOT mask credentials. Those MUST be kept out of telemetry paths upstream by `secure-configuration` and the LLM wrappers.

#### Scenario: an API key in user text is not modified

- GIVEN user text contains a credential literal
- WHEN the sanitizer runs
- THEN the literal is NOT modified
- AND upstream configuration tests are responsible for catching the bug

### Requirement: Observability — Replacement Counter

The sanitizer MUST return a per-category replacement counter alongside the transformed text. The counter MAY be logged at `DEBUG`; the alias mapping table MUST NOT be logged at `INFO` or higher.

#### Scenario: counter and alias map at correct log levels

- GIVEN the sanitizer records the alias map and per-category counts
- WHEN the application logs at `INFO`
- THEN the alias map is absent from log lines
- AND only the per-category counter MAY appear at `DEBUG`
