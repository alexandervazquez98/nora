# Exploration: HTTP/SSE Transport Support for `nora-mcp` Console Script

**GitHub issue**: #34 (`alexandervazquez98/nora`)
**Change name**: `2026-09-13-http-sse-transport`
**Artifact store**: OpenSpec

---

## Current State

NORA boots **stdio-only** today. The `nora-mcp` console script (`pyproject.toml:34` →
`nora.cli:main`) calls `mcp.run(show_banner=False)` with no transport argument, so
FastMCP defaults to stdio. There is **no `run_sse.py` or any HTTP/SSE runner shipped
in the repo** — operators wanting remote access today roll their own scripts
(precisely the pain point of #34).

### Exact locations

| File | Lines | What lives there |
|------|-------|------------------|
| `src/nora/cli.py` | 48–78 | `main()` boot sequence. `mcp.run(show_banner=False)` at **line 78** is the single line that hard-locks stdio. |
| `src/nora/cli.py` | 15–18 | Module docstring explicitly states *"Default transport is stdio (per FastMCP `mcp.run()` with no transport arg)"* — this text needs updating. |
| `src/nora/__main__.py` | 29 | Deprecation alias delegates to `nora.cli.main`; no transport awareness required (it inherits whatever `cli.main` decides). |
| `pyproject.toml` | 24–25 | `fastmcp>=3.2,<4`. `uv.lock` resolves to **FastMCP 3.4.7**. |
| `pyproject.toml` | 32–34 | `[project.scripts]` wires `nora-mcp = "nora.cli:main"`. |
| `scripts/nora-mcp.service` | 14 | `ExecStart=/opt/nora/.venv/bin/nora-mcp` — no flags, no env overrides for transport. |
| `scripts/nora-mcp.service` | 26 | `EnvironmentFile=/etc/nora/nora.env` — **only the runtime env file is sourced today**; there is no separate transport-env file. |
| `scripts/install.sh` | 334–367 | `phase_env_file()` writes `/etc/nora/nora.env` from `.env.example` with mode 0640. The signing key is injected post-copy via `sed -i`. |
| `scripts/install.sh` | 404–409 | `phase_systemd()` drops the unit, runs `daemon-reload`, and `enable --now nora-mcp`. |
| `scripts/bootstrap.sh` | 261–266 | Forwards `--force-env-file` to `install.sh`; **no awareness of transport** today. |
| `scripts/verify-install.sh` | 410–497 | `check_functional()` runs a stdio-only smoke test (initialize + tools/list + prompts/list over stdin/stdout). It does not probe HTTP/SSE. |
| `.env.example` | 1–26 | The sanitized runtime template. **No `NORA_MCP_TRANSPORT` / `_HOST` / `_PORT` variables exist** today. |
| `openspec/specs/nora-mcp-server/spec.md` | 9–23 | Requirement **`FastMCP Boot Over Stdio`** hard-codes the stdio lock: *"The server MUST boot via `FastMCP("nora")` and register exactly four `@mcp.tool` functions over stdio."* — needs a MODIFIED delta. |

### Current FastMCP API (3.4.7) — `mcp.run()` transport contract

Source: `.venv/lib/python3.12/site-packages/fastmcp/server/mixins/transport.py:75-130`.

```python
def run(
    self: FastMCP,
    transport: Transport | None = None,        # "stdio" | "http" | "sse" | "streamable-http"
    show_banner: bool | None = None,
    **transport_kwargs: Any,
) -> None:
    ...
```

`Transport` literal: `{"stdio", "http", "sse", "streamable-http"}` (line 92). Any other
value raises `ValueError("Unknown transport: {transport}")`.

HTTP-specific kwargs (from `run_http_async`, lines 258–298):

| Kwarg | Default | Notes |
|-------|---------|-------|
| `transport` | `"http"` | One of `"http"`, `"streamable-http"`, `"sse"`. **This is the kwarg of the same name forwarded from `run` — same literal set as above.** |
| `host` | `127.0.0.1` (from `fastmcp.settings.host`) | Bound for the HTTP server. |
| `port` | `8000` (from `fastmcp.settings.port`) | |
| `log_level` | settings.log_level | Lower-cased before use. |
| `path` | `settings.streamable_http_path` (=`/mcp`) or `settings.sse_path` (=`/sse`) | |
| `uvicorn_config` | `{}` | Merged on top of `{timeout_graceful_shutdown: 2, lifespan: "on", ws: "websockets-sansio"}`. |
| `middleware` | `[]` | Starlette ASGI middleware list. |
| `json_response` | `settings.json_response` | |
| `stateless_http` / `stateless` | `settings.stateless_http` | **SSE is incompatible** — line 308–309 raises `ValueError("SSE transport does not support stateless mode")`. |
| `host_origin_protection` | `"auto"` | Protects localhost-bound servers and explicit host/origin allowlists. |
| `allowed_hosts`, `allowed_origins` | `None` | |
| `sockets` | `None` | Pre-bound Uvicorn sockets. |

FastMCP's own `Settings` model (`.venv/lib/python3.12/site-packages/fastmcp/settings.py`)
uses `env_prefix="FASTMCP_"`, so the library **already reads `FASTMCP_TRANSPORT`,
`FASTMCP_HOST`, `FASTMCP_PORT` natively from the environment** without any new code.
This is important for the "scope" question below: do we add a NORA-namespaced prefix
or pass through FastMCP's?

---

## Library Support — What FastMCP 3.4.7 Actually Accepts

| Transport literal | Status in 3.4.7 | Use case |
|-------------------|------------------|----------|
| `"stdio"` | ✅ Default | Local subprocess (Claude Desktop, `verify-install.sh` smoke). |
| `"http"` | ✅ | Streamable HTTP — **alias for `"streamable-http"`**; goes through `create_streamable_http_app`. |
| `"streamable-http"` | ✅ | The MCP "Streamable HTTP" transport (the modern spec). Same code path as `"http"`. |
| `"sse"` | ✅ | Legacy Server-Sent Events. Goes through `create_sse_app` with `sse_path` + `message_path`. **Incompatible with `stateless_http=True`.** |
| anything else | ❌ | `raise ValueError("Unknown transport: {transport}")` at line 93. |

The Open WebUI consumer in #34's description connects over "Streamable-HTTP / SSE on
port 8005" — either `"http"` (preferred, modern) or `"streamable-http"` works for
that. We should not ship `"sse"` as the default, but it must be selectable for legacy
clients.

---

## Affected Areas (blast radius)

- `src/nora/cli.py` — **primary edit**. Replace `mcp.run(show_banner=False)` with
  `mcp.run(transport=..., host=..., port=..., show_banner=False)` driven by CLI args
  and/or env vars. Update the module docstring (lines 15–18). Keep the
  `FASTMCP_SHOW_SERVER_BANNER=false` setdefault.
- `src/nora/__main__.py` — no change required; alias delegates verbatim to
  `cli.main`.
- `tests/test_cli.py` — **lines 47–58 MUST be updated**. Today they assert
  `assert "mcp.run(show_banner=False)" in src` and `assert "transport=" not in src`.
  Both will fail post-change. Replace with a regression test that checks the default
  (no env, no flags) still produces `mcp.run(transport="stdio", show_banner=False)`,
  plus positive tests for env-var override and CLI-flag override.
- `tests/test_main_alias.py` — keeps passing unchanged (it boots stdio explicitly
  via subprocess stdin/stdout — no env-driven transport).
- `tests/intervention_writer/test_stdio_smoke.py` — keeps passing unchanged (same
  reason; it pins stdio directly via `subprocess` pipes).
- `scripts/nora-mcp.service` — add `EnvironmentFile=/etc/nora/nora-mcp.env` (or
  consolidate into the existing `nora.env`). Update `ExecStart` to pass through the
  chosen transport explicitly OR rely on the env file. See "Risks" below for the
  `EnvironmentFile=` vs `Environment=` tradeoff.
- `scripts/install.sh` — `phase_env_file()` becomes a loop or a two-file emit: write
  `nora.env` (existing) AND `nora-mcp.env` (new, transport-specific, mode 0640).
  Update `phase_systemd()` only if the unit template changes. The `--force-env-file`
  flag semantics need to extend to the new file (or get a new `--force-transport-env`).
- `scripts/bootstrap.sh` — forward any new install.sh flag for the transport env.
- `scripts/verify-install.sh` — `check_functional()` stays stdio-only (it asserts
  JSON-RPC frames on stdin/stdout — exactly what stdio transport produces). Add an
  optional `check_http_listener()` that probes `http://${host}:${port}/mcp` for a
  `405 Method Not Allowed` or `/mcp` GET response when `NORA_MCP_TRANSPORT` is set
  to anything non-stdio. Gate behind a new `--check-transport` flag.
- `.env.example` — append a new section documenting `NORA_MCP_TRANSPORT`,
  `NORA_MCP_HOST`, `NORA_MCP_PORT` with safe defaults (`stdio`, `127.0.0.1`, `8000`).
- `openspec/specs/nora-mcp-server/spec.md` — **MODIFIED delta** for `FastMCP Boot Over
  Stdio` requirement (lines 9–23). Also add a new requirement `Configurable Network
  Transport` with Given/When/Then scenarios for: env-var precedence, CLI-flag
  precedence, default-stdio behavior, HTTP bind smoke, SSE bind smoke.
- New tests (≥2, per TDD): `tests/test_cli_transport.py` covering
  (1) CLI flag precedence over env var, and (2) env var precedence over default.
  Plus a `test_http_transport_binds_on_configured_port` that starts `nora-mcp` on a
  free port and probes `/mcp` with `httpx` (already a venv dep, see
  `openspec/changes/archive/2026-09-06-foundation-bootstrap/explore.md:15`).
- `INSTALL.md` — update the "TLS / network exposure" note (lines 273–275) that
  today says *"NORA is stdio-only"*. Document the new transport env vars and the
  hardening story (localhost bind by default, host-origin protection on by default).
- `OPERATIONS.md` — add a "Transport modes" subsection under "Log interpretation"
  (after line 125, the `with transport 'stdio'` log line) so operators see what
  each transport's startup line looks like. Also update the `Starting MCP server`
  line table if applicable.

---

## Approaches

### Option A — `nora-mcp` parses its own CLI flags + env vars, passes through to `mcp.run()`

- Read `NORA_MCP_TRANSPORT` / `_HOST` / `_PORT` from `os.environ` (cli's job — the
  `Settings` boundary owns `.env`, but transport is a `mcp.run()` arg, not a Settings
  field, so reading it from `os.environ` here is consistent with `FASTMCP_SHOW_SERVER_BANNER`
  setdefault on line 28).
- Add `argparse` with `--transport`, `--host`, `--port` flags that override env vars.
- Pass through to `mcp.run(transport=..., host=..., port=..., show_banner=False)`.
- Pros: explicit, testable, follows the precedence convention (CLI > env > default).
  Idempotent reconfigure: rotate env file, restart, done.
- Cons: adds ~30 LoC to `cli.py`. Three new env vars to document.
- **Effort: Low–Medium.** Mirrors existing patterns (cli.py already reads env via
  `os.environ.setdefault`).

### Option B — Drop NORA-specific env vars; rely entirely on FastMCP's built-in `FASTMCP_*` prefix

- Set the transport by exporting `FASTMCP_TRANSPORT=http`, `FASTMCP_HOST=...`,
  `FASTMCP_PORT=...` in the systemd `EnvironmentFile`. No code change in `cli.py`
  beyond the existing `mcp.run(show_banner=False)`.
- Pros: zero new code; FastMCP's `Settings` already merges them. Operators familiar
  with FastMCP get it for free.
- Cons: leaks the FastMCP internal naming into NORA's deployment contract; couples
  NORA's operator docs to the library's. The `tests/test_cli.py:47-58` test STILL
  passes (no `transport=` in source), so the existing regression test is happy — but
  we lose the ability to add CLI flags cleanly. Doesn't match what the issue asks for
  (issue specifies `NORA_MCP_TRANSPORT`).
- **Effort: Very low** — but contradicts #34's expected behavior literally.

### Option C — Hybrid: `NORA_MCP_*` env vars for the operator contract; CLI flags for one-shot overrides; FastMCP receives the resolved values

- Default `NORA_MCP_TRANSPORT`/`_HOST`/`_PORT` (issue's exact names). If unset, fall
  through to FastMCP's `FASTMCP_TRANSPORT`/`_HOST`/`_PORT`. If neither set, stdio
  on `127.0.0.1:8000` (FastMCP's defaults — though stdio ignores host/port).
- Pros: matches the issue's spec exactly. Honors FastMCP's built-in contract for
  operators who already know it.
- Cons: two layers of fallback to document; if a user sets both `NORA_MCP_TRANSPORT`
  and `FASTMCP_TRANSPORT` the precedence rules need to be explicit.
- **Effort: Medium.** More nuance = more tests, more docs.

### Option D — Update `Settings` (the Pydantic model) to add `nora_mcp_transport`, `nora_mcp_host`, `nora_mcp_port`

- Pros: keeps the contract surface in one place (`src/nora/config.py`); benefits
  from the existing `.env` / process-env precedence handling (`.env` beats process
  env per `Settings.settings_customise_sources`).
- Cons: this is the wrong abstraction layer. `Settings` is for NORA domain config
  (catalog paths, signing keys, HITL TTL). Transport is a FastMCP runtime arg, not
  a domain setting. Mixing them inflates `Settings` and breaks the "Settings = NORA
  domain config" mental model.
- **Effort: Medium–High.** Bloats the env-file template, fights existing patterns.

---

## Recommendation

**Option A** with a twist toward Option C: parse `NORA_MCP_TRANSPORT`,
`NORA_MCP_HOST`, `NORA_MCP_PORT` from `os.environ` in `cli.py` (mirroring the
existing `FASTMCP_SHOW_SERVER_BANNER` setdefault pattern at line 28). Add `argparse`
with `--transport`, `--host`, `--port` that override env vars. Precedence:
**CLI > env > default (stdio)**. Skip Option D entirely — `Settings` is the wrong
seam. Skip Option B as the sole path — issue #34 explicitly names the `NORA_MCP_*`
prefix, so the contract must use it.

The installer (`scripts/install.sh`) writes `/etc/nora/nora-mcp.env` with mode 0640
containing the three variables (default `NORA_MCP_TRANSPORT=stdio`,
`NORA_MCP_HOST=127.0.0.1`, `NORA_MCP_PORT=8005` — port 8005 matches the Open WebUI
example in #34). The systemd unit gains a second `EnvironmentFile=` directive
pointing at `nora-mcp.env`. **Default transport stays `stdio`** — backwards
compatibility for the existing `verify-install.sh` stdio smoke and every operator who
has an in-flight stdio-based dashboard. Operators flip to HTTP by editing one env
var and running `sudo systemctl restart nora-mcp`.

---

## Risks & Open Questions for Proposal Phase

1. **Default transport flip vs. backward compat** — STRONG recommendation to keep
   `stdio` as the default. Flipping to `http` would break the existing
   `verify-install.sh::check_functional` JSON-RPC-stdio smoke (every existing
   operator's install would suddenly fail health checks) and any in-flight
   subprocess-based orchestrators. The proposal MUST default to `stdio` and document
   the one-line operator action to enable HTTP.

2. **Env var precedence** — Two viable rules:
   (a) **CLI > env > default** (standard Unix tool convention; matches `argparse`
       defaults).
   (b) **env > CLI > default** (matches systemd unit pattern, where `EnvironmentFile`
       usually wins over command-line flags set in the unit — because the unit is the
       authoritative deployment artifact).
   Recommend (a). The orchestrator should confirm.

3. **`EnvironmentFile=` vs `Environment=` in the systemd unit** — `EnvironmentFile=`
   keeps secrets (signing key) and transport config in operator-editable files; the
   unit template stays clean. `Environment=` directives embed values directly in the
   unit, harder to rotate. **Recommend keeping `EnvironmentFile=`** for transport too,
   matching the existing `nora.env` pattern. Tradeoff: the operator must remember
   `systemctl daemon-reload` is NOT required when only the env file changes (env
   files are re-read on every service start), but it IS required when the unit file
   itself changes (e.g., adding the new `EnvironmentFile=` line).

4. **`tests/test_cli.py:47-58` regression lock** — the existing assertion
   `assert "transport=" not in src` will fail the moment we add `transport=` to
   `mcp.run()`. The test must be rewritten, not deleted. The new regression MUST
   pin: "with no env and no CLI flag, `cli.main` invokes `mcp.run(transport="stdio",
   show_banner=False)`". The `tests/test_main_alias.py` and `test_stdio_smoke.py`
   tests both boot via subprocess over stdio with no env tweaks, so they continue
   to verify the default-stdio path end-to-end.

5. **`tests/installer/test_install.py:359-451` no-echo-signing-key guard** — the
   new `phase_env_file()` writes a second file (`nora-mcp.env`). If a future change
   ever passes transport values through `printf` or `echo`, the guard must still
   hold. The new file contains no secrets (transport + bind address only), so this
   is low-risk but worth a dedicated regression.

6. **Spec delta shape** — `openspec/specs/nora-mcp-server/spec.md` currently has
   `FastMCP Boot Over Stdio` as a hard requirement. The cleanest delta is:
   - **MODIFIED** `FastMCP Boot Over Stdio` → rename to `Configurable MCP Transport`
     and broaden to "MUST boot via `FastMCP("nora")` and MUST support stdio (default),
     Streamable HTTP (`"http"` / `"streamable-http"`), and SSE (`"sse"`) selected via
     `NORA_MCP_TRANSPORT` env var or `--transport` CLI flag, defaulting to stdio".
   - **ADDED** `EnvironmentFile Wiring for Network Transport` scenario requiring
     `/etc/nora/nora-mcp.env` to be written by `install.sh` with the three vars at
     mode 0640 owned by `nora:nora`.

7. **FastMCP version drift** — the pin is `>=3.2,<4`. The current lock is `3.4.7`.
   If `mcp.run()`'s `**transport_kwargs` ever rename a kwarg (e.g., `host` →
   `bind_host`), our explicit pass-through breaks silently. Worth a one-line comment
   in `cli.py` noting the FastMCP-version pinning.

8. **`FASTMCP_HOST=0.0.0.0` exposure** — enabling HTTP transport at all means the
   default `127.0.0.1` is safe, but an operator could set `NORA_MCP_HOST=0.0.0.0`
   and suddenly NORA is on the wire. The proposal should require a warning log when
   binding to a non-loopback address (mirrors the FastMCP `host_origin_protection`
   auto behavior but more explicit). This is a security-aware hardening question;
   flag it for the user.

---

## Reference Patterns Already in the Repo

- **Env-file precedence** — `src/nora/config.py:163-182` (`Settings.settings_customise_sources`)
  already establishes the rule "init kwargs > `.env` > process env". We do NOT need to
  replicate that here — transport config is read directly via `os.environ.get(...)`
  after `argparse` resolution, which gives CLI > env > default naturally.
- **systemd `EnvironmentFile=`** — `scripts/nora-mcp.service:26` already uses the
  pattern. We extend it, not replace it.
- **Installer file mode** — `scripts/install.sh:350` uses
  `install -m 0640 -o '${USER_NAME}' -g '${USER_NAME}'` for `nora.env`; mirror that
  for `nora-mcp.env`.
- **Port default in script** — `scripts/verify-install.sh:476` hard-codes the
  expected tool list as a single string; analogous `httpx` probe for `/mcp` would
  fit the same "string-comparison" style.
- **AST-guard test pattern** — `tests/test_cli.py:73-99` shows the AST-walk pattern
  used for "cli.py must not import LLM/journal". Reuse the same pattern for any
  new "cli.py must not construct `LLMProvider`" guards.
- **Subprocess stdio smoke pattern** — `tests/test_main_alias.py:95-167` and
  `tests/intervention_writer/test_stdio_smoke.py:50-150` both show the
  `subprocess.Popen` + JSON-RPC-over-stdin pattern. Use the same pattern for a new
  `test_http_transport_smoke.py` with `httpx.AsyncClient` against `/mcp`.

---

## Ready for Proposal

**Yes** — exploration is complete. Recommended next phase: **sdd-propose**. The
proposal should:

1. Confirm Option A (NORA-namespaced env vars + argparse CLI flags, CLI > env > stdio).
2. Confirm `stdio` stays the default (no backwards-compat break).
3. Decide between `EnvironmentFile=` (recommended) vs `Environment=` for the
   systemd unit.
4. Decide on `NORA_MCP_PORT` default (`8005` to match Open WebUI example, or
   `8000` to match FastMCP's default — flag for user).
5. Decide whether to issue one MODIFIED + one ADDED delta in
   `openspec/changes/.../specs/nora-mcp-server/spec.md` or to split into a new
   capability folder (e.g., `nora-mcp-transport`). Recommend the former (in-place
   delta) — `nora-mcp-server` is the right owner.
6. Confirm the test rewrite shape for `tests/test_cli.py:47-58`.

The orchestrator should flag **risk #1 (default transport flip)** and
**risk #3 (EnvironmentFile vs Environment)** to the user before proposal write —
both are decisions the user should make explicitly, not be inferred.
