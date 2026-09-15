# nora-mcp-server Specification

## Purpose

Defines how NORA boots the FastMCP server over stdio, registers the `nora_health` tool as the first MCP-facing surface, and enforces the hard constraint that all logging goes to stderr — never stdout, which is reserved for the JSON-RPC stream. This capability is the Phase 1 deliverable that lets an operator's LLM agent discover NORA's version, active provider, connectivity, and `.env` load status.

## Requirements

### Requirement: FastMCP Boot With Configurable Transport

The server MUST boot via `FastMCP("nora")` and SHALL support four transport modes — `stdio` (default), `http`, `streamable-http`, `sse` — selected by CLI flags (`--transport`, `--host`, `--port`, `--path`, `--stateless-http/--no-stateless-http`) overriding `NORA_MCP_TRANSPORT`, `NORA_MCP_HOST` (default `127.0.0.1`), `NORA_MCP_PORT` (default `8005`), `NORA_MCP_PATH` (default `/mcp`), `NORA_MCP_STATELESS_HTTP` (default `false`; MUST NOT be `true` with `sse`). Precedence SHALL be CLI args > env vars > stdio default. FastMCP MUST stay pinned to `>=3.2,<4`. Boot MUST NOT construct `LLMProvider`, MUST NOT call `init_session_journal`, MUST NOT register `_AutoTraceMiddleware`. An invalid `NORA_MCP_TRANSPORT` or `--transport` value MUST cause `cli.main()` to write the bad value AND the four valid options to stderr AND exit non-zero; `mcp.run()` MUST NOT be invoked. (Previously: stdio-only, hard-coded by `mcp.run(show_banner=False)` at `src/nora/cli.py:78`; no env or CLI surface.)

#### Scenario: server registers and runs over stdio (default)

- GIVEN no `NORA_MCP_*` env vars AND no CLI flags
- WHEN `cli.main(argv=sys.argv)` runs
- THEN `mcp.run(transport="stdio", show_banner=False)` is invoked AND NORA code does not write to stdout

#### Scenario: pinned FastMCP version

- GIVEN `pyproject.toml`
- WHEN its dependencies are listed
- THEN `fastmcp` is pinned to `>=3.2,<4`

#### Scenario: env vars default to stdio when unset

- GIVEN no `NORA_MCP_*` env vars AND no CLI flags
- WHEN `cli.main(argv=sys.argv)` runs
- THEN `transport="stdio"` is selected AND `mcp.run(transport="stdio", show_banner=False)` is invoked

#### Scenario: CLI args override env vars

- GIVEN `NORA_MCP_TRANSPORT=stdio` AND `NORA_MCP_PORT=8000` in the process env
- WHEN `cli.main(argv=["nora-mcp", "--transport=http", "--port=9000"])` runs
- THEN `mcp.run(transport="http", host=..., port=9000, path=..., show_banner=False)` is invoked

#### Scenario: invalid transport value fails loud with helpful stderr

- GIVEN `NORA_MCP_TRANSPORT=garbage`
- WHEN `cli.main(argv=sys.argv)` runs
- THEN exit code is non-zero AND stderr names the bad value `garbage` AND stderr lists the valid options `stdio`, `http`, `streamable-http`, `sse` AND `mcp.run()` is never invoked

#### Scenario: `stateless_http=true` with `sse` is rejected

- GIVEN `NORA_MCP_TRANSPORT=sse` AND `NORA_MCP_STATELESS_HTTP=true`
- WHEN `cli.main(argv=sys.argv)` runs
- THEN exit code is non-zero AND stderr names the incompatibility AND `mcp.run()` is never invoked

### Requirement: Stderr-Only Logging

Every log line emitted by NORA MUST go to stderr. No code path under `src/nora/` MAY write to stdout.

#### Scenario: stderr receives a startup log line

- GIVEN the server starts
- WHEN stderr is captured
- THEN a log line names the boot surface

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
- WHEN `correlate_sector_interference(...)` is invoked
- THEN the literal is replaced by a synthetic alias

#### Scenario: structured fields bypass the sanitizer

- GIVEN `search_intervention_history` returns its typed top-level fields
- WHEN the response is serialised
- THEN `intervention_id`, `target_ip`, `stage`, `status`, `timestamp_unix` are NOT run through the sanitizer

### Requirement: Security Boundary — No Secrets in Tool Responses

Every MCP tool MUST NOT include any secret value from `Settings` in any response field, error message, or log line.

#### Scenario: signing key never appears in tool response

- GIVEN `Settings.nora_oid_catalog_signing_key` is set
- WHEN any of the four tools is invoked
- THEN no response field contains the signing key value

#### Scenario: signing key never appears in error messages

- GIVEN the driver raises an `InventoryError`
- WHEN the tool returns its error message
- THEN the message does NOT contain the signing key value

### Requirement: Observability — Stderr Tool Diagnostics

Each tool invocation MUST emit one structured log line on stderr with tool name, duration, and outcome.

#### Scenario: tool call emits a structured log line

- GIVEN `snmp_get_pmp450i_radio_metrics` is invoked
- WHEN the call completes
- THEN one stderr line is emitted with tool name, duration, and outcome

### Requirement: R-NEW-1 — Four `@mcp.tool` Registrations

The server SHALL register exactly twelve `@mcp.tool`-decorated functions on the global `mcp = FastMCP("nora")`: one driver + three intervention memory + `save_intervention_record` + six radio-link tools (`snmp_get_ap_summary`, `snmp_get_frame_utilization`, `snmp_get_sm_table`, `snmp_get_sm_detailed_diagnostics`, `snmp_run_spectrum_analysis`, `snmp_migrate_radio_frequency`) + `register_device(host: str, community: str, validate: bool = True) -> DeviceRecord`. All twelve names SHALL be re-exported in `__all__`. (Previously: exactly eleven registrations.)

#### Scenario: server module exports the twelve tool names

- GIVEN `src/nora/server.py` imports the driver + three intervention callables + the writer + six radio-link callables + the `register_device` wrapper
- WHEN the twelve names are imported from `nora.server`
- THEN the imports succeed AND all twelve names appear in `nora.server.__all__`

#### Scenario: `tools/list` over stdio returns twelve tools in the registered order

- GIVEN the server booted via `mcp.run()` over stdio
- WHEN a client calls `tools/list`
- THEN the response contains the twelve tool names AND their `inputSchema` matches each registered signature AND `register_device`'s schema declares `host: str`, `community: str`, `validate: bool` (default `true`)

### Requirement: R-NEW-2 — Sanitizer Bound at Tool Boundary

Every free-text field in the twelve tool responses SHALL pass through `Sanitizer.sanitize(...)` before serialisation. Structured top-level fields SHALL bypass. The `register_device` payload's `community` field SHALL be masked at the Pydantic boundary via `SecretStr` (rendered as `"**********"` in `model_dump(mode="json")`). (Previously: eleven tools; now twelve.)

#### Scenario: free-text fields in the radio-link tools are sanitized

- GIVEN a tool response whose free-text field contains the literal `192.0.2.10`
- WHEN the response is serialised
- THEN the literal is replaced by a synthetic alias AND typed scalars (`carrier_frequency_mhz`, `result["rolled_back"]`) are byte-identical

#### Scenario: free-text fields in `register_device` error messages are sanitized

- GIVEN a `DeviceUnreachable` error whose message contains the literal `192.0.2.10`
- WHEN the tool serialises the response
- THEN the literal is replaced by a synthetic alias AND typed `DeviceRecord` fields (`device_id`, `host`) are byte-identical

#### Scenario: credential masking on `register_device` success payload

- GIVEN a successful `register_device("192.0.2.10", "MEXI2-BB-RW")` call
- WHEN `device.model_dump(mode="json")` runs
- THEN the `community` field renders as `"**********"` AND the literal `MEXI2-BB-RW` does NOT appear

### Requirement: R-NEW-3 — Hard Read-Only Contract (AST Guard)

The three intervention tools SHALL NOT mutate any file. `tests/intervention_memory/test_no_writes.py` SHALL fail any commit that introduces a writable file operation under `src/nora/intervention_memory/`. The driver tool is out of scope for this guard.

#### ADDED Scenario: the AST read-only guard fails on an injected write

- GIVEN a developer adds `Path("/tmp/x").write_text("x")` inside `src/nora/intervention_memory/storage.py`
- WHEN `uv run pytest tests/intervention_memory/test_no_writes.py` runs
- THEN the test exits non-zero AND the failure message identifies `(storage.py, <line>, "write_text")`

### Requirement: R-NEW-4 — One-Way Cross-Capability Dependency Direction

`src/nora/server.py` MAY import from `nora.intervention_memory.tools`, `nora.intervention_writer.writer`, `nora.drivers.snmp_pmp450i.driver`, `nora.hitl.tokens`, `nora.drivers.interface`, `nora.drivers.resolver`. No module under those packages SHALL import from `nora.server`. (Previously: two consumer packages; now six.)

#### Scenario: consumer packages do not import from `nora.server`

- GIVEN every `.py` under `src/nora/intervention_memory/`, `src/nora/intervention_writer/`, and `src/nora/drivers/`
- WHEN a static grep scans for `from nora.server` or `import nora.server`
- THEN zero matches are found

### Requirement: `python -m nora` Deprecation Alias

`python -m nora` MUST emit a `DeprecationWarning` whose message contains `will be removed in the next minor release` and MUST delegate to the `nora-mcp` boot sequence, exposing the identical four-tool surface.

#### Scenario: alias emits the deprecation warning and boots the thin surface

- GIVEN the package is installed in editable mode
- WHEN `python -m nora` is invoked as a subprocess
- THEN stderr contains one line matching `DeprecationWarning` and `will be removed in the next minor release` AND the four-tool surface is reachable via JSON-RPC

#### Scenario: alias and `nora-mcp` expose identical tool lists

- GIVEN both entry points are reachable
- WHEN each is asked for its `tools/list` over stdio
- THEN the two responses contain the same four tool names in the same order

### Requirement: No Boot-Time LLM or Journal Injection

The canonical `nora-mcp` boot (`src/nora/cli.py`) MUST wire `Settings() -> PromptRegistry.from_settings -> OidCatalogRegistry.verify_all -> Inventory.from_yaml -> set_driver -> mcp.run(show_banner=False)`. Boot MUST NOT construct `LLMProvider`, call `init_session_journal`, call `build_provider`, or register `_AutoTraceMiddleware`.

#### Scenario: cli.py has no LLM/journal/middleware wiring

- GIVEN `src/nora/cli.py`
- WHEN its source is scanned for `build_provider`, `init_session_journal`, `_AutoTraceMiddleware`, `register_auto_trace_middleware`
- THEN zero matches exist

#### Scenario: server module has no LLMProvider import

- GIVEN `src/nora/server.py` and `src/nora/__main__.py`
- WHEN their imports are scanned for `LLMProvider` and `build_provider`
- THEN zero matches exist
### Requirement: R-NEW-5 — 5th MCP Tool Registration (`save_intervention_record`)

The global `mcp = FastMCP("nora")` instance in `src/nora/server.py` MUST
register a 5th `@mcp.tool`-decorated function named
`save_intervention_record` that delegates 1:1 to
`nora.intervention_writer.writer.save_intervention_record`. The wrapper
MUST be a thin delegate with no logic of its own (mirroring the existing
four read-only wrappers), so the function is callable from non-MCP code
(issue #15 library reuse from `snmp_get_ap_summary`). The 5th name MUST
appear in `nora.server.__all__` after the four existing tool names. The
wrapper MUST NOT require an HITL approval token — record-keeping blast-
radius is recoverable by deleting the file; HITL scope is reserved for #15's
destructive RF migration per `openspec/config.yaml` `rules.specs`. Input
schema, error codes, and security guarantees are defined by the
`intervention-writer` capability (see
`openspec/specs/intervention-writer/spec.md`).

#### Scenario: `save_intervention_record` appears in the registered tool list

- GIVEN the server booted via `mcp.run()` over stdio
- WHEN a client calls `tools/list`
- THEN the response contains `save_intervention_record` AND its `inputSchema` matches the writer's payload contract

#### Scenario: the wrapper delegates 1:1 with no logic drift

- GIVEN `nora.intervention_writer.writer.save_intervention_record` is monkeypatched to return `{"delegated": True}`
- WHEN the MCP wrapper is invoked with a payload
- THEN the patched library function is called exactly once with that payload AND the wrapper returns `{"delegated": True}` unchanged

#### Scenario: stdio call writes a record under the configured dir

- GIVEN `NORA_INTERVENTIONS_DIR=/tmp/test/` AND a valid payload over stdio
- WHEN the MCP tool `save_intervention_record` is invoked
- THEN a `INT-<ticket>-<ip>-<unix>-<hex>.json` file appears under `/tmp/test/` AND the response is `{"status": "OK", "intervention_id": "<stem>"}`

#### Scenario: the wrapper rejects without an HITL token

- GIVEN an MCP client invokes `save_intervention_record` over stdio with no approval token
- WHEN the wrapper executes
- THEN no `HITL_REQUIRED` error is raised AND the record is written immediately
### Requirement: Server-Level `instructions` (Updated) — HITL Advertised

The `instructions` string SHALL advertise that `snmp_migrate_radio_frequency` requires an explicit HITL approval token and that autonomous device mutation is rejected without it. The Zero-Leakage + intervention-memory framing is preserved. (Previously: no destructive tool was exposed; HITL was unadvertised.)

#### Scenario: instructions text names HITL on the migration tool

- GIVEN `mcp = FastMCP("nora", instructions=_SERVER_INSTRUCTIONS)`
- WHEN the instructions string is inspected
- THEN it mentions `snmp_migrate_radio_frequency` AND names the `AutonomousMutationRejected` contract


### Requirement: R-NEW-6 — Tool-Registration Guard (Catalog Re-Signed)

`src/nora/cli.py` SHALL run a registration guard that walks every function registered against the global `mcp` instance. The guard MUST raise a typed `UncataloguedToolError` if the tool name is absent from `OidCatalogRegistry.REQUIRED_OIDS` for every catalogued `(vendor, model, firmware)` triple. The guard runs ONCE at boot; the rejection MUST abort before `mcp.run(show_banner=False)`. After the PMP 450i catalog re-sign, every re-signed baseline (`15.2.1`, `15.3.0`, `25.1.0`) carries `"register_device": ["sysDescr"]` in its `tools` envelope AND `sysDescr: "1.3.6.1.2.1.1.1.0"` in its `oids` map; each `hmac_sha256` SHALL pass `OidCatalogRegistry.verify_all`. The `_ALLOWED_UNCATALOGUED_TOOLS` allow-list at `src/nora/server.py:626` SHALL no longer contain `snmp_get_pmp450i_radio_metrics` (its envelope is added to all three baselines by the same re-sign); the remaining four entries are unchanged. (Previously: `register_device` uncatalogued; `snmp_get_pmp450i_radio_metrics` was a fifth allow-list entry.)

#### Scenario: an uncatalogued `@mcp.tool` is rejected at boot

- GIVEN a developer adds `@mcp.tool def snmp_get_rogue_metric(device_id: str): ...` with no catalog entry
- WHEN `cli.main()` runs the registration guard
- THEN `UncataloguedToolError` is raised naming `(tool_name, reason="no OID catalog entry")` AND `mcp.run()` is never invoked

#### Scenario: every re-signed PMP 450i baseline HMAC verifies

- GIVEN the three catalog files at `data/oid-catalogs/cambium/pmp450i/{15.2.1,15.3.0,25.1.0}.json` after `scripts/sign_catalog.py` re-sign (parametrized across the three versions)
- WHEN `OidCatalogRegistry.verify_all()` runs at boot
- THEN all three HMACs verify AND no `CatalogSignatureError` is raised AND `mcp.run(show_banner=False)` proceeds

#### Scenario: every re-signed baseline's tools envelope includes register_device and sysDescr OID

- GIVEN the three re-signed catalog files (parametrized across the three versions)
- WHEN `OidCatalogRegistry` loads `(cambium, pmp450i, <version>)`
- THEN the `tools` map contains `"register_device": ["sysDescr"]` AND the `oids` map contains `"sysDescr": "1.3.6.1.2.1.1.1.0"`

#### Scenario: `register_device` passes the registration guard via its catalog envelope

- GIVEN `register_device` is `@mcp.tool`-decorated AND present in all three re-signed catalogs
- WHEN `cli.main()` runs the registration guard
- THEN `register_device` is accepted without an allow-list entry AND `mcp.run(show_banner=False)` proceeds

#### Scenario: `snmp_get_pmp450i_radio_metrics` no longer requires the allow-list

- GIVEN the three re-signed catalogs now carry `snmp_get_pmp450i_radio_metrics` in the `tools` envelope
- WHEN `_ALLOWED_UNCATALOGUED_TOOLS` is inspected at `src/nora/server.py:626`
- THEN `"snmp_get_pmp450i_radio_metrics"` is NOT in the frozenset AND the four remaining entries are unchanged
### Requirement: Systemd Loads Transport Env File

`scripts/install.sh` SHALL write `/etc/nora/nora-mcp.env` at mode `0640` owned by `nora:nora`, populated from `.env.mcp.example` with default `NORA_MCP_TRANSPORT=stdio`. `scripts/nora-mcp.service` SHALL contain a second `EnvironmentFile=/etc/nora/nora-mcp.env` directive alongside the existing `EnvironmentFile=/etc/nora/nora.env` at line 26. Env-only changes SHALL take effect on the next `systemctl restart nora-mcp` without `daemon-reload`; unit-file changes SHALL require `daemon-reload`.

#### Scenario: `/etc/nora/nora-mcp.env` is shipped with stdio default at mode 0640

- GIVEN a fresh install on a supported host
- WHEN `install.sh` runs to completion
- THEN `/etc/nora/nora-mcp.env` exists AND its mode is `0640` AND ownership is `nora:nora` AND its content includes `NORA_MCP_TRANSPORT=stdio`

#### Scenario: systemd unit reads the transport env file on every restart

- GIVEN the `nora-mcp.service` unit is active
- WHEN an operator edits `/etc/nora/nora-mcp.env` to `NORA_MCP_TRANSPORT=http` AND runs `sudo systemctl restart nora-mcp`
- THEN the new transport is applied AND `daemon-reload` was NOT required for the env-only change
### Requirement: R-NEW-7 — `nora hitl mint` Sub-Command

`src/nora/__main__.py` SHALL dispatch on `argv[1]`: `mcp` → existing
`cli.main()` boot; `hitl` → new
`nora hitl mint --operator-id <id> --ttl-seconds <n>` CLI handler that
emits a signed `HitlApprovalToken` JSON payload on stdout. Unknown
sub-commands print help to stderr AND exit non-zero. `nora hitl mint`
exits non-zero on missing/invalid arguments (`--operator-id` required,
`--ttl-seconds` optional, default
`Settings.nora_hitl_token_ttl_seconds`).

#### Scenario: `nora` with no args still boots MCP (back-compat pinned)

- GIVEN the operator runs `nora` (or `python -m nora`) without a
  sub-command
- WHEN the process starts
- THEN `mcp.run(show_banner=False)` is invoked AND the twelve-tool
  surface is reachable via JSON-RPC (deprecation-alias tests
  `test_main_alias.py` and `test_integration_boot.py:323` stay green)

#### Scenario: `nora hitl mint` emits a signed token

- GIVEN `Settings.nora_hitl_signing_key` is a non-empty `SecretStr` AND
  `Settings.nora_tool_specs_dir` is configured AND the operator runs
  `nora hitl mint --operator-id alice --ttl-seconds 900`
- WHEN the sub-command handler runs
- THEN stdout carries one JSON line containing `operator_id`,
  `issued_at`, `expires_at`, `token`, AND `signature` (non-empty)
  AND exit code is `0`

#### Scenario: `nora hitl mint` rejects invalid args

- GIVEN the operator runs `nora hitl mint` without `--operator-id`
- WHEN the sub-command handler runs
- THEN exit code is non-zero AND stderr names the missing argument
  AND no token is emitted

#### Scenario: `nora` with unknown sub-command exits non-zero with help

- GIVEN the operator runs `nora bogus`
- WHEN the dispatcher parses argv
- THEN exit code is non-zero AND stderr lists the valid sub-commands
  AND `mcp.run()` is never invoked

### Requirement: R-NEW-8 — Spectrum Tool Wire Shape Gains `operator_confirmed`

The `snmp_run_spectrum_analysis` `@mcp.tool` registration SHALL
declare `operator_confirmed: bool = False` in its FastMCP wire
schema. The runtime gate (raise `Tier1ClearanceRequired` on False) is
owned by `pmp450i-radio-tools`; this capability owns only the wire-
shape contract.

#### Scenario: tools/list declares operator_confirmed

- GIVEN the server booted via `mcp.run()` over stdio
- WHEN a client calls `tools/list` and inspects the
  `snmp_run_spectrum_analysis` entry
- THEN `inputSchema.properties.operator_confirmed.type == "boolean"`
  AND the default is `false`

### Requirement: R-NEW-9 — Boot Wires Multi-Dir Prompt Registry

`cli.main()` SHALL call `PromptRegistry.from_settings(settings)` after
`Settings()` is loaded AND before `mcp.run(show_banner=False)`. The
`from_settings` implementation reads `Settings.nora_tool_specs_dir`
and scans both `src/nora/prompts/` and `docs/tool_specs/`. Boot SHALL
NOT abort solely because the tool-spec dir is absent (see
`prompt-registry` R8).

#### Scenario: cli.main wires PromptRegistry.from_settings

- GIVEN `cli.main()` source
- WHEN it is scanned for `from_settings`
- THEN at least one `PromptRegistry.from_settings(settings)` call is
  present AND it precedes `mcp.run(show_banner=False)`

#### Scenario: registration guard still passes for all twelve tools

- GIVEN the boot path runs the existing R-NEW-6 registration guard
- WHEN the guard inspects the twelve `@mcp.tool` registrations
- THEN `UncataloguedToolError` is NOT raised AND `mcp.run()` proceeds
  (no regression from R-NEW-6)

### Requirement: R-NEW-10 — `nora-mcp` Alias Untouched

The existing `nora-mcp = "nora.cli:main"` console-script entry SHALL
remain unchanged. New functionality is exposed ONLY through the
`nora` dispatcher (Approach A from proposal §"Prompt composition
approach"). A separate `nora-hitl` console script is **rejected**
(OPEN QUESTION 3 — design must explicitly reject it with rationale).

#### Scenario: nora-mcp entry point is preserved

- GIVEN `pyproject.toml`'s `[project.scripts]` table
- WHEN the table is parsed
- THEN `nora = "nora.__main__:main"` AND `nora-mcp =
  "nora.cli:main"` are present AND no `nora-hitl` entry exists

## Open Questions (deferred to design)

- **Q1** — composition mechanism (inline marker vs runtime lookup).
  Owned by `prompt-registry`.
- **Q3** — `__main__.py` refactor and `nora-hitl` exposure. Default
  NO; design owns.
- **Q4** — tool-spec front-matter schema. Owned by `prompt-registry`.

## Cross-References

`tool-service-impact-tiers` (CLI shape driver), `hitl-approval-tokens`
(`nora hitl mint` output contract), `pmp450i-radio-tools`
(`operator_confirmed` wire field declared here, gate enforced there),
`prompt-registry` (multi-dir scan wired at boot),
`secure-configuration` (two new settings read at boot).
## Cross-References

Depends on `intervention-memory` capability (read-only consumer). The three new `@mcp.tool` registrations (`search_intervention_history`, `get_device_lifecycle_summary`, `correlate_sector_interference`) delegate to pure functions in `nora.intervention_memory.tools`; the dependency direction is one-way (`nora-mcp-server` → `intervention-memory`, never the reverse). Sanitizer contract inherited from `intervention-memory` R9 / `session-journal` R6. Auto-trace recording inherited from `session-journal` R2 — no middleware change was required for this delta.
