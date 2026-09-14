# Delta for nora-mcp-server

> Modified by `2026-09-13-http-sse-transport` — broadens the canonical `FastMCP Boot Over Stdio` requirement (lines 9–23 of `openspec/specs/nora-mcp-server/spec.md`) into a configurable transport contract driven by `NORA_MCP_*` env vars + CLI flags, default unchanged to `stdio` for backwards compat. Adds a separate requirement covering installer + systemd wiring for `/etc/nora/nora-mcp.env`. Issue #34.

## MODIFIED Requirements

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

## ADDED Requirements

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