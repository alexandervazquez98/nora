# Delta for nora-mcp-server — Intervention-Memory MCP Surface

> Additive only. No existing requirement from
> `openspec/specs/nora-mcp-server/spec.md` is modified, removed, or
> renamed. The change adds three new read-only tools on the global
> `FastMCP("nora")` instance and inherits the existing typed-return,
> stderr-only, sanitizer-boundary, no-secrets, and one-log-per-tool
> contracts.

## ADDED Requirements

### Requirement: R-NEW-1 — Three Read-Only Tools on the Global MCP Instance

The server SHALL register exactly three new `@mcp.tool`-decorated functions
on the global `mcp = FastMCP("nora")` instance defined in
`src/nora/server.py`:
`search_intervention_history`, `get_device_lifecycle_summary`, and
`correlate_sector_interference`. Each tool SHALL delegate its body to the
corresponding pure function in
`nora.intervention_memory.tools`. The three names SHALL be re-exported in
`__all__` for test discoverability. The auto-trace middleware registered
via `register_auto_trace_middleware()` SHALL record every invocation of
the three new tools under the same `session-journal` R2 contract — no
middleware change is required.

#### ADDED Scenario: server module exports the three new tool names

- GIVEN `src/nora/server.py` imports the three pure functions from `nora.intervention_memory.tools`
- WHEN `from nora.server import search_intervention_history, get_device_lifecycle_summary, correlate_sector_interference` runs
- THEN the imports succeed
- AND the three names appear in `nora.server.__all__`

#### ADDED Scenario: MCP tool wrappers delegate to the pure library functions

- GIVEN a `set_runtime_state(settings=fake_settings, provider=fake_provider)` call
- WHEN the registered `search_intervention_history(target_ip="10.0.0.5")` MCP tool is invoked
- THEN `nora.intervention_memory.tools.search_intervention_history` is called exactly once with the same kwargs
- AND the MCP wrapper returns the library result unchanged

#### ADDED Scenario: auto-trace middleware records a call to the new tool

- GIVEN `register_auto_trace_middleware(journal)` has run
- WHEN the registered `search_intervention_history(...)` MCP tool is invoked
- THEN the journal's trace gains one `SessionStep` with `tool == "search_intervention_history"`
- AND `outcome == "success"`

### Requirement: R-NEW-2 — Sanitizer Bound at Tool Boundary

Every free-text field in the three new tools' output SHALL pass through
`Sanitizer.sanitize(...)` before serialization. Structured top-level
fields (`intervention_id`, `target_ip`, `stage`, `status`,
`timestamp_unix`) SHALL bypass per the existing `session-journal` R6
contract. The bypass list SHALL be inherited as-is from `session-journal`
R6 — no new bypass keys are added by this delta.

#### ADDED Scenario: free-text fields in `search_intervention_history` output are sanitized

- GIVEN a record whose `record_name` contains the literal `10.53.12.4`
- WHEN `search_intervention_history(target_ip=...)` is invoked via the MCP tool
- THEN the returned list contains a dict whose `record_name` does NOT include `10.53.12.4`
- AND it includes a `RADIO_NODE_*` alias

#### ADDED Scenario: structured top-level fields bypass the sanitizer

- GIVEN a record whose `intervention_id == "INT-1-10.0.0.5-1234567-XYZ"`
- WHEN `search_intervention_history(...)` returns the record via the MCP tool
- THEN `record["intervention_id"]` is byte-identical to the on-disk value
- AND `record["timestamp_unix"]` is byte-identical (int)
- AND `record["stage"]` is byte-identical (enum string)

### Requirement: R-NEW-3 — Hard Read-Only Contract (AST Guard)

The three new tools SHALL NOT mutate any file. The test
`tests/intervention_memory/test_no_writes.py` SHALL fail any commit that
introduces a writable file operation under `src/nora/intervention_memory/`.
The expected failure mode — if a future contributor adds a write tool
without removing the AST test — is that the build breaks. This is a
test-time guard only; the server SHALL NOT enforce the read-only contract
at runtime. The auto-trace middleware records a tool call whose body
raises `PermissionError` with `outcome == "error"`.

#### ADDED Scenario: the AST read-only guard fails on an injected write

- GIVEN a developer adds `Path("/tmp/x").write_text("x")` inside `src/nora/intervention_memory/storage.py`
- WHEN `uv run pytest tests/intervention_memory/test_no_writes.py` runs
- THEN the test exits non-zero
- AND the failure message identifies `(storage.py, <line>, "write_text")`

#### ADDED Scenario: a hypothetical write tool records an error outcome

- GIVEN a hypothetical tool whose body raises `PermissionError("writes are forbidden")`
- WHEN the auto-trace middleware wraps the call
- THEN the journal's trace gains one `SessionStep` with `outcome == "error"`
- AND `result_summary` contains the sanitized message

### Requirement: R-NEW-4 — One-Way Cross-Capability Dependency Direction

The dependency between `nora-mcp-server` and `intervention-memory` SHALL
be one-way: `src/nora/server.py` MAY import from
`nora.intervention_memory.tools` to wire the `@mcp.tool` registrations.
The reverse direction SHALL NOT exist: no module under
`src/nora/intervention_memory/` (other than `shim_webui.py` itself) SHALL
import from `nora.server`, `nora.drivers`, or any other NORA capability
that would couple the package to the MCP wiring. The `shim_webui.py`
mirror is also a one-way consumer — it imports from
`nora.intervention_memory.tools` and is consumed by external deploy
scripts (out of repo), not by `nora-mcp-server`.

#### ADDED Scenario: `nora-mcp-server` imports from `intervention-memory.tools`

- GIVEN `src/nora/server.py` adds `from nora.intervention_memory.tools import search_intervention_history as _search, ...`
- WHEN the server module is imported
- THEN no import error is raised
- AND the three `@mcp.tool` registrations bind to the imported callables

#### ADDED Scenario: `intervention-memory` does not import from `nora.server`

- GIVEN every `.py` under `src/nora/intervention_memory/` except `shim_webui.py`
- WHEN a static grep scans for `from nora.server`, `import nora.server`, or `from nora.drivers`
- THEN zero matches are found

#### ADDED Scenario: `shim_webui.py` is consumed only by external deploy scripts

- GIVEN a static grep across `src/nora/` for `from nora.intervention_memory.shim_webui`
- WHEN the scan runs
- THEN the ONLY match is the file itself (the import statement at the top of `shim_webui.py` for self-typing)
- AND no production module under `src/nora/intervention_memory/` imports from `shim_webui`
