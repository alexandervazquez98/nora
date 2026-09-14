# Design: HTTP/SSE Transport for nora-mcp

## Technical Approach

Make transport a first-class boot argument instead of a stdio hard-code. `src/nora/cli.py` builds a `TransportConfig` (frozen dataclass) by resolving `argparse` CLI flags → `NORA_MCP_*` env vars → defaults, validates it, and forwards to `mcp.run(transport=..., host=..., port=..., path=..., stateless_http=...)`. `scripts/install.sh` writes `/etc/nora/nora-mcp.env` from a new committed `.env.mcp.example` (0640 `nora:nora`); `scripts/nora-mcp.service` adds a second `EnvironmentFile=` directive. Default stays `stdio` — existing stdio deploys, the `check_functional()` JSON-RPC smoke, and the `test_main_alias.py` / `test_stdio_smoke.py` subprocess tests stay green untouched. Issue #34.

## Architecture Decisions

| # | Option | Tradeoff | Decision |
|---|--------|----------|----------|
| 1 | `argparse` over hand-rolled `sys.argv` slicing | Hand-rolled is ~10 LoC but no `--help`, no `choices=`, no `type=` coercion, untestable | **`argparse`**. Testability via `argv=` injection. |
| 2 | Frozen `dataclass` for `TransportConfig` vs `pydantic.BaseModel` | Pydantic adds `BaseSettings` ergonomics but conflates with `Settings` (domain config); this is a transport-resolution struct, not a config model | **Frozen `dataclass`**. Mirrors local value-objects; no extra dep. |
| 3 | Read env via `os.environ.get` in `cli.py` vs extend `Settings` | `Settings` is NORA-domain config (catalogs, signing key, HITL TTL) — transport is a `mcp.run()` arg, not a domain setting | **`os.environ.get` in `cli.py`** (mirrors existing `FASTMCP_SHOW_SERVER_BANNER` setdefault at line 28). |
| 4 | Second `EnvironmentFile=` line vs consolidating into `nora.env` | Consolidated file mixes secrets + transport; second file keeps operator surface narrow | **Second `EnvironmentFile=/etc/nora/nora-mcp.env`** after line 26. |
| 5 | `httpx` in tests vs `urllib.request` vs `requests` | `httpx` is already in `uv.lock` (transitive via FastMCP); `tests/test_driver_airgap.py` already mocks `httpx.get`; `urllib` requires manual Content-Length | **`httpx`** test-only import. Add to `[dependency-groups].dev` for explicit pinning. |
| 6 | HTTP smoke gated behind `--check-http` flag | Always-on adds noise for stdio-only operators; opt-in matches `check_functional()` opt-out pattern (`--skip-functional`) | **Opt-in `--check-http`**; default off. |

## Boot Sequence

```
Settings()                       # unchanged (lines 51)
set_runtime_state(settings)      # unchanged
PromptRegistry / OidCatalogRegistry / Inventory / set_driver  # unchanged
verify_tools_are_catalogued(...) # unchanged (line 66) — catalog guard MUST run before transport kickoff
register_tool_log_middleware()   # unchanged
parser.parse_args(argv)          # NEW — reads CLI flags (--transport, --host, --port, --path, --stateless-http/--no-stateless-http)
_resolve_transport_config(args)  # NEW — CLI > env > defaults → TransportConfig
_validate_transport(config)      # NEW — raises ValueError on bad transport or sse+stateless_http
mcp.run(transport=..., host=..., port=..., path=..., stateless_http=..., show_banner=False)  # MODIFIES line 78
```

Transport validation runs **after** `verify_tools_are_catalogued` (catalog guard at line 66 is non-negotiable) and **before** `mcp.run()`. If validation fails, `mcp.run()` is never invoked.

## Module: `src/nora/cli.py` — Code Shape

```python
from dataclasses import dataclass
from typing import Sequence

VALID_TRANSPORTS = frozenset({"stdio", "http", "streamable-http", "sse"})

@dataclass(frozen=True)
class TransportConfig:
    transport: str       # one of VALID_TRANSPORTS
    host: str            # default "127.0.0.1"
    port: int            # default 8005
    path: str            # default "/mcp"
    stateless_http: bool # default False; forced False when transport == "sse"

def _build_parser() -> argparse.ArgumentParser: ...    # prog="nora-mcp"
def _parse_transport_args(argv: Sequence[str] | None = None) -> argparse.Namespace: ...
def _resolve_transport_config(args: argparse.Namespace) -> TransportConfig: ...
def _validate_transport(config: TransportConfig) -> None: ...   # raises ValueError

def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_transport_args(argv if argv is not None else sys.argv[1:])
    config = _resolve_transport_config(args)
    try:
        _validate_transport(config)
    except ValueError as exc:
        sys.stderr.write(f"nora-mcp: {exc}\n"); sys.exit(2)
    # ...existing boot sequence unchanged...
    mcp.run(transport=config.transport, host=config.host, port=config.port,
            path=config.path, stateless_http=config.stateless_http, show_banner=False)
```

**`sys.exit(2)`**: matches argparse's own exit code on usage errors — semantically correct. **Test seam**: `monkeypatch.setattr(cli.mcp, "run", lambda **kw: None)` is the right seam (FastMCP class instance is `cli.mcp`, not a `MagicMock`); patching `mcp.run` rather than re-instantiating `FastMCP` keeps tests fast.

## File Changes

| File | Action | Description |
|------|--------|-------------|
| `src/nora/cli.py` | Modify | Add `TransportConfig`, `_parse_transport_args`, `_resolve_transport_config`, `_validate_transport`; replace `mcp.run(show_banner=False)` at line 78 with the kwarg form; module docstring at lines 15–18 drops the "stdio-only" assertion. |
| `.env.mcp.example` | Create | New template committed at repo root (mode 0644 in repo). |
| `scripts/install.sh` | Modify | Add `FORCE_TRANSPORT_ENV=0` flag (line ~78); add `--force-transport-env` parse (line ~200) + `usage` entry (line ~162); add `phase_env_file_mcp()` after `phase_env_file()` (line ~367) — same install-line idiom as line 351, mode 0640, `nora:nora`. |
| `scripts/nora-mcp.service` | Modify | After line 26 (`EnvironmentFile=/etc/nora/nora.env`), add `EnvironmentFile=/etc/nora/nora-mcp.env`. |
| `scripts/bootstrap.sh` | Modify | Add `--force-transport-env` flag (line ~122 case) + `FORCE_TRANSPORT_ENV="false"` default (line ~72); forward in `install_args` (line ~265). |
| `scripts/verify-install.sh` | Modify | Add `--check-http` flag (line ~195) + `CHECK_HTTP=0` (line ~71); add `check_http_listener()` after `check_functional()` (line ~497) — reads `/etc/nora/nora-mcp.env`, skips silently when transport=stdio, otherwise TCP-probes `127.0.0.1:$NORA_MCP_PORT` + optional `curl` GET on `$NORA_MCP_PATH`. |
| `tests/test_cli.py` | Modify | Replace lines 47–58 (`test_cli_main_invokes_mcp_run_without_transport_arg`) with the four new assertions below; add `test_cli_help`, `test_stateless_http_with_sse_rejected`. |
| `tests/test_http_transport_smoke.py` | Create | Subprocess boot on `127.0.0.1:8765` (avoids prod 8005), `httpx` probe against `/mcp`, teardown. 1 happy-path test. |

## `.env.mcp.example` Template

```
# /etc/nora/nora-mcp.env — nora-mcp transport configuration
# Loaded by systemd on every `systemctl restart nora-mcp` (no daemon-reload needed).
# Default: stdio. Uncomment the http block to expose FastMCP over HTTP.

NORA_MCP_TRANSPORT=stdio
# NORA_MCP_HOST=127.0.0.1
# NORA_MCP_PORT=8005
# NORA_MCP_PATH=/mcp
# NORA_MCP_STATELESS_HTTP=false
```

## Test Strategy

| Layer | What | Approach |
|-------|------|----------|
| Unit | Default stdio; env sets transport; CLI overrides env; invalid exits 2 with stderr naming bad value + 4 options; `--help` exit 0; `sse` + `stateless_http=true` rejected | `monkeypatch.setattr(cli.mcp, "run", mock)`, `capsys.readouterr()`, `monkeypatch.setenv()`. Six new tests in `tests/test_cli.py`. |
| Integration | HTTP boot binds `127.0.0.1:8765`, responds at `/mcp` | Subprocess spawn + `httpx.AsyncClient`; teardown kills child. One new file `tests/test_http_transport_smoke.py`. |
| Regression | `test_main_alias.py`, `tests/intervention_writer/test_stdio_smoke.py` still pass | Unchanged — they boot stdio via subprocess with no env. |
| Installer | `--force-transport-env` writes `nora-mcp.env` 0640 owned by nora | Add to `tests/installer/test_install.sh` mirror. |
| Verifier | `check_http_listener()` skips when stdio, probes when non-stdio | Mirror to `tests/verifier/test_verify_install.sh`. |

Coverage: `src/nora/cli.py` ≥ 95% (project threshold 85% per `openspec/config.yaml:116`); new code 100% covered.

## Threat Matrix

`N/A` — design does not introduce new routing, shell, subprocess, VCS/PR automation, executable-file classification, or process-integration boundaries *beyond* what existing installer / verifier scripts already exercise. Subprocess boot in `test_http_transport_smoke.py` mirrors the existing `tests/test_main_alias.py:95-167` pattern. `--force-transport-env` flag is a flag-forwarding extension to `bootstrap.sh`, not a new injection vector (existing `--force-env-file` already passed the same audit).

## Migration / Rollout

- **Migration**: existing installs get `nora-mcp.env` with `NORA_MCP_TRANSPORT=stdio` on next `install.sh` run; stdio default preserves every behavior. Operators opt into HTTP by uncommenting the http block + `sudo systemctl restart nora-mcp`.
- **Rollback**: `git revert <merge-sha>` + `uv sync`; operator either re-edits `nora-mcp.env` to `stdio` or removes the second `EnvironmentFile=` line + `sudo systemctl daemon-reload && sudo systemctl restart nora-mcp`.

## Open Questions

- [ ] **Add `httpx` to `[dependency-groups].dev`?** Currently transitive via FastMCP lockfile entry; explicit pin removes "transitive flake" risk. Recommend **yes**.
- [ ] **`phase_env_file_mcp()` vs. extend `phase_env_file()` with a path arg?** Two-file writer reads cleaner; extending with a path arg keeps the signing-key injection (line 359–365) centralized. **Recommend new function** — signing key never touches `nora-mcp.env`.
- [ ] **`NORA_MCP_PORT` default: 8005 (issue #34 example) or 8000 (FastMCP default)?** Spec locks 8005; record rationale in INSTALL.md to head off the FastMCP-docs surprise.
- [ ] **CI for `--check-http`?** Add a tox/pytest job that runs `nora-mcp --transport=http --port=8765` then `httpx.get("/mcp")`? **Recommend yes** — closes the loop on the new test file.

## Out of Scope

TLS termination (delegated to reverse proxy per `INSTALL.md`). Auth/middleware/CORS beyond FastMCP defaults. `FASTMCP_*` passthrough (NORA's operator contract stays `NORA_MCP_*`). Renaming the console script. Auto-flip of default transport on upgrade.