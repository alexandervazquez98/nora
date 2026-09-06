# nora-mcp-server Specification

## Purpose

Defines how NORA boots the FastMCP server over stdio, registers the `nora_health` tool as the first MCP-facing surface, and enforces the hard constraint that all logging goes to stderr — never stdout, which is reserved for the JSON-RPC stream. This capability is the Phase 1 deliverable that lets an operator's LLM agent discover NORA's version, active provider, connectivity, and `.env` load status.

## Requirements

### Requirement: FastMCP Boot Over Stdio

The server MUST boot via `FastMCP("nora")`, MUST register tools via `@mcp.tool`, and MUST run over stdio by default. FastMCP MUST be pinned to `>=3.2,<4` (v3 stable; v4 ships breaking changes to background tasks).

#### Scenario: server registers and runs over stdio

- GIVEN the server module imports FastMCP and registers `nora_health`
- WHEN `mcp.run()` is called
- THEN the server listens on stdio
- AND NORA code does not write to stdout

#### Scenario: pinned FastMCP version

- GIVEN `pyproject.toml`
- WHEN its dependencies are listed
- THEN `fastmcp` is pinned to `>=3.2,<4`

### Requirement: `nora_health` Tool Contract

`nora_health` MUST return a JSON-serialisable object with four fields: `version`, `active_provider`, `connectivity`, `env_loaded`. The tool MUST invoke `provider.complete(...)` with a fixed system prompt and MUST NOT mutate any device state.

#### Scenario: healthy server returns all four fields

- GIVEN the active provider is reachable
- WHEN `nora_health` is invoked
- THEN the response includes all four fields with `connectivity: ok`

#### Scenario: provider unreachable reports unavailable

- GIVEN the active provider is unreachable
- WHEN `nora_health` is invoked
- THEN `connectivity` reports unavailable
- AND the tool does NOT raise

#### Scenario: `.env` not present is reported

- GIVEN no `.env` file is present
- WHEN `nora_health` is invoked
- THEN `env_loaded == false`

### Requirement: Stderr-Only Logging

Every log line emitted by NORA MUST go to stderr. No code path under `src/nora/` MAY write to stdout. The ruff rule banning `print(...)` in `src/nora/` is part of this contract.

#### Scenario: stderr receives a startup log line

- GIVEN the server starts
- WHEN stderr is captured
- THEN a log line names the active provider and model

#### Scenario: stdout is reserved for JSON-RPC

- GIVEN the server is processing a request
- WHEN stdout is captured for 1 second
- THEN the only bytes on stdout are JSON-RPC frames

#### Scenario: a stray `print()` is caught at lint time

- GIVEN `src/nora/server.py` contains `print("debug")`
- WHEN `ruff check .` runs
- THEN the violation is reported and exit code is non-zero

### Requirement: Telemetry Sanitizer Boundary

Every free-text value leaving the process via an MCP tool response MUST pass through the telemetry sanitizer before serialisation. Typed structured fields MAY opt out.

#### Scenario: free-text fields are sanitized

- GIVEN an upstream error message contains a private IPv4 literal
- WHEN `nora_health` includes that message
- THEN the literal is replaced by an alias

#### Scenario: structured fields bypass the sanitizer

- GIVEN `nora_health` returns the four typed fields
- WHEN the response is serialised
- THEN those fields are NOT run through the sanitizer

### Requirement: Security Boundary — No Secrets in Tool Responses

Every MCP tool MUST NOT include any secret value from `Settings` in any response field, error message, or log line.

#### Scenario: API key never appears in tool response

- GIVEN `Settings.gemini_api_key` is set
- WHEN `nora_health` is invoked
- THEN no field contains the API key value

#### Scenario: API key never appears in error messages

- GIVEN the active provider fails to initialise
- WHEN the tool returns an error message
- THEN the message does NOT contain any secret value

### Requirement: Edge Cases

Malformed JSON-RPC frame and provider timeout MUST be handled without crashing the server.

#### Scenario: malformed input is rejected with a JSON-RPC error

- GIVEN a malformed payload arrives on stdin
- WHEN the server processes it
- THEN a JSON-RPC parse error is returned on stdout
- AND the server continues running

#### Scenario: provider timeout is bounded

- GIVEN the active provider hangs longer than the configured timeout
- WHEN `nora_health` invokes it
- THEN the call returns `connectivity: unavailable` within the timeout

### Requirement: Observability — Stderr Tool Diagnostics

Each tool invocation MUST emit one structured log line on stderr with tool name, duration, and outcome. The line MUST NOT include any secret value.

#### Scenario: tool call emits a structured log line

- GIVEN `nora_health` is invoked
- WHEN the call completes
- THEN one stderr line is emitted with tool name, duration, and outcome
