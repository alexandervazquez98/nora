# Proposal: HTTP/SSE Transport for nora-mcp

## Intent

`nora.cli:main` (`src/nora/cli.py:78`) hardcodes stdio. Issue #34: under systemd, stdio receives EOF; remote MCP clients (OpenChat WebUI over Streamable-HTTP/SSE on port 8005) cannot connect without hand-rolled runners. Make transport configurable via `NORA_MCP_TRANSPORT`/`_HOST`/`_PORT` env vars + CLI flags; ship installer + systemd wiring for HTTP; keep stdio default. No library upgrade — FastMCP 3.4.7 already accepts the kwargs.

## Scope

**In**: argparse + env fallback in `cli.py`; new `/etc/nora/nora-mcp.env` (0640); second `EnvironmentFile=` in systemd unit; MODIFIED + ADDED scenarios in `openspec/specs/nora-mcp-server/spec.md`; gated HTTP smoke in `verify-install.sh`; test rewrite of `tests/test_cli.py:47-58`. **Out**: TLS, reverse-proxy, auth/middleware/CORS beyond FastMCP defaults.

## Capabilities

- **Modified**: `nora-mcp-server` — rename `FastMCP Boot Over Stdio` → `FastMCP Boot With Configurable Transport`; broaden to stdio (default), `"http"`/`"streamable-http"`, `"sse"` via env or `--transport`.
- **New**: None. Installer + systemd wiring is runtime concern of the modified capability.

## Approach

- **Default `stdio`**, precedence CLI > env > stdio. Backwards compat with `verify-install.sh:410-497` JSON-RPC smoke + existing systemd deploys.
- **Env vars** (#34 names): `NORA_MCP_TRANSPORT` ∈ {`stdio`,`http`,`streamable-http`,`sse`}; `NORA_MCP_HOST` (`127.0.0.1`); `NORA_MCP_PORT` (`8005`); `NORA_MCP_PATH` (`/mcp`); `NORA_MCP_STATELESS_HTTP` (`false`; SSE n/a).
- **CLI flags**: `--transport`, `--host`, `--port`, `--path`, `--stateless-http/--no-stateless-http`.
- **Invalid transport**: catch `ValueError` from `mcp.run()` in `cli.py`; stderr names bad value + valid options; exit non-zero.
- **Installer**: `phase_env_file()` writes `nora-mcp.env` from new `.env.mcp.example` (0640 `nora:nora`); stdio default; `nora.env` untouched. `--force-transport-env` flag.
- **systemd**: second `EnvironmentFile=/etc/nora/nora-mcp.env` after line 26; `ExecStart` flag-less.
- **verify-install.sh**: `check_functional()` stays stdio. New `check_http_listener()` (gated `--check-http`) probes `/mcp` TCP-accept when transport ≠ stdio.
- **Spec delta**: MODIFIED `FastMCP Boot Over Stdio` (broaden). ADDED: CLI args override env; env defaults stdio; `nora-mcp.env` wired at 0640. KEEP pinned-version scenario (`>=3.2,<4`).
- **Tests (TDD-first)**: REPLACE `test_cli.py:47-58` with 4 assertions (default stdio; env sets; CLI overrides; invalid raises); ADD `test_cli_help`; ADD `test_http_transport_smoke.py` (subprocess + httpx).
- **Open Questions**: (1) `cli.main(argv=)` for testability? **Yes**. (2) `--transport help`? **Yes**. (3) `.env.mcp.example` vs heredoc? **`.env.mcp.example`**. (4) ADR for `NORA_MCP_*` naming? **Yes, one short ADR**.

## Affected Areas

- `src/nora/cli.py` — replace lines 73–78 with argparse + env fallback
- `openspec/specs/nora-mcp-server/spec.md` — rename + broaden `FastMCP Boot Over Stdio` (9–23)
- `scripts/nora-mcp.service` — add second `EnvironmentFile=` after line 26
- `scripts/install.sh` — extend `phase_env_file()` (334–367); add `--force-transport-env`
- `scripts/bootstrap.sh`, `scripts/verify-install.sh` — forward flag; add `check_http_listener()` + `--check-http`
- `tests/test_cli.py`, `tests/test_http_transport_smoke.py` — 4 new assertions + httpx probe
- `.env.example`, `.env.mcp.example`, `INSTALL.md`, `OPERATIONS.md` — document `NORA_MCP_*` + transport modes

## Risks

- **Stdio deploys flip transport on upgrade** (Low) — default `nora-mcp.env` is stdio; `nora.env` untouched; stdio smoke stays primary.
- **Invalid transport silently defaults** (Low) — `cli.py` catches `ValueError`, stderr names bad value + valid options, exit non-zero.
- **FastMCP deprecates `sse`** (Med) — pin `>=3.2,<4`; flag SSE as legacy; ADR for upgrade path.

## Rollback Plan

1. `git revert <merge-sha>` + `uv sync`.
2. Installer leaves `nora.env` untouched.
3. Operator edits `nora-mcp.env` → `NORA_MCP_TRANSPORT=stdio` (or removes second `EnvironmentFile=` line).
4. `sudo systemctl daemon-reload && sudo systemctl restart nora-mcp`.
5. `verify-install.sh` stdio smoke = rollback sentinel.

## Success Criteria

- [ ] `nora-mcp` no env/CLI boots stdio (existing `check_functional` passes)
- [ ] `nora-mcp --transport=http` binds `127.0.0.1:8005`; `curl .../mcp` returns MCP response
- [ ] `NORA_MCP_TRANSPORT=http` + `systemctl restart nora-mcp` exposes HTTP
- [ ] `NORA_MCP_TRANSPORT=garbage` exits non-zero with stderr naming bad value + valid options
- [ ] 4 new `test_cli.py` assertions + `test_http_transport_smoke.py` pass
- [ ] `uv run ruff check . && uv run mypy --strict src/nora` clean
- [ ] No new `pyproject.toml` deps
