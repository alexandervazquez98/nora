# intervention-writer Specification

## Purpose

Atomic-write companion to `intervention-memory`. Sibling capability, not a
relaxation of reader R2. Validates against `InterventionMemoryRecord`,
sanitises free-text fields before serialisation, builds
`INT-<ticket>-<ip>-<unix>-<hex>.json`, writes via `tmp + fsync + os.replace`
under `Settings.nora_interventions_dir`. Library function exposed at
package level (for #15 reuse) AND as 5th `@mcp.tool`. No HITL gate.

## Requirements

### Requirement: W1 — Filename Template

`INT-<ticket>-<ip>-<unix>-<hex>.json`. `<ticket>`/`<ip>` match
`[A-Za-z0-9_-]+`. `<unix>` is 10-digit epoch. `<hex>` is 6 random hex
chars. 5-retry budget before `DUPLICATE_INTERVENTION_ID`.

#### Scenario: happy-path filename

- GIVEN payload with `ticket_number="TKT-001"`, `target_ip="10.0.0.1"`
- WHEN `save_intervention_record` runs at unix `1700000000`
- THEN on-disk file is `INT-TKT-001-10.0.0.1-1700000000-<6hex>.json`

#### Scenario: path-traversal in `target_ip` rejected

- GIVEN `target_ip="../../etc/passwd"`
- WHEN the regex runs
- THEN `INVALID_INPUT` is returned, no file written

### Requirement: W2 — Path Containment

`Path.resolve().is_relative_to(base_dir.resolve())`. Out-of-tree resolutions
return `PATH_TRAVERSAL_DETECTED`.

#### Scenario: forward slash in `ticket_number` rejected

- GIVEN `ticket_number="OPS/PROD-12"`
- WHEN the regex runs
- THEN `INVALID_INPUT` is returned before resolve

#### Scenario: symlink pointing outside dir refused

- GIVEN `interventions_dir/escape_link -> /tmp`
- WHEN `resolve()` runs
- THEN `is_relative_to` is False, write refused

### Requirement: W3 — Atomic Write Semantics

Pattern `write(tmp) → fsync(tmp.fileno()) → os.replace`. `.tmp` suffix
mismatch means reader's `*.json` glob skips torn files. Writer sweeps
`*.json.tmp` older than 3600 s per call.

#### Scenario: killed process leaves `.tmp` only

- GIVEN prior interrupted write `INT-X.json.tmp` 90 minutes old
- WHEN reader's glob runs
- THEN `.tmp` is skipped, next writer call sweeps it

#### Scenario: successful write leaves only `.json`

- GIVEN a clean dir
- WHEN `save_intervention_record` returns OK
- THEN listing shows one `.json`, no `.tmp`

### Requirement: W4 — Sanitization On Write

Walk free-text fields through `Sanitizer.sanitize(...)` BEFORE
serialisation. Walked fields match `intervention-memory` R9 (record-level
free text + `network_equipment.system_name` + `hardware_band` + every
subscriber `note`); bypass scalars match R9 (typed ids, ip, stage, status,
timestamps, ticket).

#### Scenario: free-text IPv4 literal masked on disk

- GIVEN `findings_and_dictamen == "node 192.168.1.1 was down"` validated
- WHEN writer writes
- THEN file contains `RADIO_NODE_X`, not `192.168.1.1`

#### Scenario: bypass field not masked

- GIVEN `ticket_number == "INT-TKT-001-10.0.0.1-1234567-abcdef"`
- WHEN sanitisation runs
- THEN file value is byte-identical

### Requirement: W5 — Schema Validation Before Write

`InterventionMemoryRecord.model_validate(payload)` MUST run BEFORE any
side-effect. `ValidationError` → `INVALID_PAYLOAD` with sanitised dicts.
No file written on failure.

#### Scenario: valid payload writes

- GIVEN payload that passes `model_validate`
- WHEN writer returns
- THEN response is `{"status":"OK","intervention_id":"<stem>"}`, one `.json` on disk

#### Scenario: invalid `stage` returns INVALID_PAYLOAD

- GIVEN `stage == "PRE_MIGRATIO"`
- WHEN writer validates
- THEN `INVALID_PAYLOAD` returned, no write

### Requirement: W6 — 5th MCP Tool Registration

Library function exposed as `@mcp.tool(name="save_intervention_record")` on
global `mcp` in `src/nora/server.py`. Thin delegate for #15 library use. 5th
name in `nora.server.__all__`. Wrapper MUST NOT re-sanitise (W4 masked at write).

#### Scenario: `save_intervention_record` in tool list

- GIVEN server booted via `mcp.run()`
- WHEN client calls `tools/list`
- THEN response includes `save_intervention_record`, its `inputSchema` matches

#### Scenario: stdio call writes under configured dir

- GIVEN `NORA_INTERVENTIONS_DIR=/tmp/test/`, valid payload over stdio
- WHEN wrapper runs
- THEN `INT-...json` appears, wrapper returns OK

### Requirement: W7 — Banned-Imports Boundary

Banned inbound: `pytest`, `_pytest`, `monkeypatch`, `nora.server`,
`nora.drivers`, `nora.intervention_memory.shim_webui`. Allowed:
`nora.intervention_memory.models`, `nora.config.Settings`. AST scan
asserts zero matches.

#### Scenario: AST scan rejects `import nora.server`

- GIVEN contributor adds `import nora.server` to `writer.py`
- WHEN `test_banned_imports.py` runs
- THEN test fails naming `(writer.py, <line>, "nora.server")`

### Requirement: W8 — No HITL Approval Gate

No HITL gate. Records land immediately. Blast-radius recoverable by file
deletion (SCOPE.md §4.C). HITL reserved for #15 RF migration.

#### Scenario: stdio lands with no approval token

- GIVEN client invokes `save_intervention_record` over stdio (no token)
- WHEN wrapper executes
- THEN record is written, no `HITL_REQUIRED` raised

## Cross-References

- `intervention-memory` — schema source; R2 read-only invariant unchanged.
- `telemetry-sanitizer` — W4 fields match `intervention-memory` R9.
- `nora-mcp-server` — 5th `@mcp.tool` in `src/nora/server.py` (see change delta).
