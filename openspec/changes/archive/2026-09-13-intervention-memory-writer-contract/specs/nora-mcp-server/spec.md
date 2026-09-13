# Delta for nora-mcp-server

## ADDED Requirements

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
