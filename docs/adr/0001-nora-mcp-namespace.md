# ADR-0001: `NORA_MCP_*` namespace for transport configuration

## Status

Accepted — 2026-09-13. Implements the operator contract for issue #34.

## Context

FastMCP 3.4.7 already reads `FASTMCP_TRANSPORT`, `FASTMCP_HOST`,
`FASTMCP_PORT`, `FASTMCP_PATH`, `FASTMCP_STATELESS_HTTP` natively from
the environment (`Settings(env_prefix="FASTMCP_")`). Two options exist
for the operator-facing env-var contract:

1. **Pass through `FASTMCP_*`** — zero new code; operators familiar with
   FastMCP get it for free.
2. **Adopt `NORA_MCP_*`** — mirror the existing `NORA_OID_*`,
   `NORA_INTERVENTIONS_*`, `NORA_HITL_*` namespaces.

## Decision

Use `NORA_MCP_*`. Read directly via `os.environ.get` inside `cli.py`
(NOT extended into `Settings`), with precedence CLI flag > env > stdio
default.

## Rationale

- **Precedent**: every existing NORA namespace follows `NORA_DOMAIN_*`.
  Adding `FASTMCP_*` would be the first library-leaked prefix and break
  the "operators see one NORA contract" mental model.
- **Library coupling**: `FASTMCP_*` ties NORA's deployment contract to
  FastMCP's internal naming. If FastMCP renames `FASTMCP_*` → `MCP_*`
  upstream, every operator's `nora-mcp.env` silently stops working.
  `NORA_MCP_*` is ours to keep.
- **Issue contract**: GitHub issue #34 explicitly names `NORA_MCP_*`.
- **`Settings` boundary**: `Settings` is NORA-domain config (catalogs,
  signing key, HITL TTL). Transport is a `mcp.run()` runtime argument,
  not a domain setting. Mixing them inflates `Settings` and breaks the
  "Settings = NORA domain config" rule. Read transport from
  `os.environ` in `cli.py` — same pattern as the existing
  `FASTMCP_SHOW_SERVER_BANNER` setdefault at the top of the module.

## Consequences

- New env vars: `NORA_MCP_TRANSPORT`, `_HOST`, `_PORT`, `_PATH`,
  `_STATELESS_HTTP`. Default `stdio` for backwards compatibility.
- CLI flags `--transport / --host / --port / --path /
  --stateless-http / --no-stateless-http` override env.
- Installer ships `/etc/nora/nora-mcp.env` (mode 0640) from
  `.env.mcp.example`. systemd reads it on every restart — no
  `daemon-reload` needed for env-only changes.
- Verification: `scripts/verify-install.sh --check-http` adds an opt-in
  HTTP probe (default off, so stdio operators see no regression).

## Alternatives considered

- **Pass through `FASTMCP_*`** — rejected: leaks library naming.
- **Extend `Settings`** — rejected: wrong abstraction layer.
- **No env vars, CLI-only** — rejected: violates #34's spec.