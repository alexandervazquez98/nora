"""Canonical NORA MCP entry point.

Wires the runtime together and hands control to FastMCP. Invoked via
the `nora-mcp` console script (see `[project.scripts]` in
`pyproject.toml`). `python -m nora` is a deprecated alias that
delegates to this module.

Boot sequence:

    Settings() -> set_runtime_state(settings) ->
    PromptRegistry.from_settings -> OidCatalogRegistry.verify_all ->
    Inventory.from_yaml -> set_driver(Pmp450iDriver(...)) ->
    register_tool_log_middleware -> mcp.run(transport=..., ...)

Default `stdio`. Override via `NORA_MCP_TRANSPORT` env var or
`--transport` CLI flag. Valid transports: `stdio`, `http`,
`streamable-http`, `sse`. Backwards-compatible with every existing
stdio-based MCP client (Claude Desktop, Open WebUI running locally,
etc.).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from typing import Literal, Sequence, cast

# Disable FastMCP's stderr banner and PyPI update probe BEFORE the
# `fastmcp` module is imported. These default to noisy and the PyPI call
# is unwanted telemetry from an MCP server that owns stderr.
os.environ.setdefault("FASTMCP_SHOW_SERVER_BANNER", "false")

from nora.config import Settings  # noqa: E402
from nora.drivers.inventory import Inventory  # noqa: E402
from nora.drivers.oid_catalog import OidCatalogRegistry  # noqa: E402
from nora.drivers.registry import set_driver  # noqa: E402
from nora.drivers.snmp_pmp450i import Pmp450iDriver  # noqa: E402
from nora.prompts.registry import PromptRegistry  # noqa: E402
from nora.server import (  # noqa: E402
    configure_logging,
    mcp,
    register_tool_log_middleware,
    set_prompt_registry,
    set_runtime_state,
    verify_tools_are_catalogued,
)

logger = logging.getLogger("nora.cli")

# ---------------------------------------------------------------------------
# Transport configuration — operator contract lives here so a future change
# to FastMCP's `mcp.run()` signature lands in ONE place. The four-mode set
# is the literal FastMCP 3.4.7 accepts (`stdio`, `http`, `streamable-http`,
# `sse`); see `design.md` "Library Support" for the citation.
# ---------------------------------------------------------------------------

VALID_TRANSPORTS: frozenset[str] = frozenset({"stdio", "http", "streamable-http", "sse"})

# Literal alias mirroring `VALID_TRANSPORTS` so mypy can narrow the
# `transport` field at the `mcp.run()` call site (FastMCP's signature is
# `Literal["stdio","http","sse","streamable-http"] | None`).
TransportLiteral = Literal["stdio", "http", "streamable-http", "sse"]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8005
DEFAULT_PATH = "/mcp"
DEFAULT_STATELESS_HTTP = False


@dataclass(frozen=True)
class TransportConfig:
    """Resolved boot configuration for `mcp.run()`.

    Built by `_resolve_transport_config` (CLI > env > defaults) and
    validated by `_validate_transport`. Frozen so a config cannot be
    mutated after validation — the value object is a one-shot record.
    """

    transport: str
    host: str
    port: int
    path: str
    stateless_http: bool


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI parser. `prog='nora-mcp'` so `--help` says the right thing."""
    parser = argparse.ArgumentParser(
        prog="nora-mcp",
        description=(
            "Boot the NORA MCP server. Default transport is stdio; "
            "override with NORA_MCP_TRANSPORT or --transport."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=sorted(VALID_TRANSPORTS),
        default=None,
        help=(
            "Transport mode. One of %(choices)s. Overrides NORA_MCP_TRANSPORT "
            "if both are set. Default is stdio (with no env var)."
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help="HTTP bind host (overrides NORA_MCP_HOST). Ignored for stdio.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="HTTP bind port (overrides NORA_MCP_PORT). Ignored for stdio.",
    )
    parser.add_argument(
        "--path",
        default=None,
        help="HTTP path the MCP server listens on (overrides NORA_MCP_PATH).",
    )
    parser.add_argument(
        "--stateless-http",
        dest="stateless_http",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Stateless HTTP mode for http/streamable-http. Incompatible "
            "with --transport=sse (FastMCP rejects)."
        ),
    )
    return parser


def _parse_transport_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse argv through `_build_parser`. Returns the namespace."""
    parser = _build_parser()
    return parser.parse_args(argv)


def _resolve_transport_config(args: argparse.Namespace) -> TransportConfig:
    """Resolve transport config: CLI flag > env var > default.

    Defaults: transport=`stdio`, host=`127.0.0.1`, port=`8005`, path=`/mcp`,
    stateless_http=`false`. Each layer overrides the layer below.
    """
    env = os.environ

    transport = (
        args.transport if args.transport is not None else env.get("NORA_MCP_TRANSPORT", "stdio")
    )
    host = args.host if args.host is not None else env.get("NORA_MCP_HOST", DEFAULT_HOST)
    port_str = args.port if args.port is not None else env.get("NORA_MCP_PORT")
    port = int(port_str) if port_str is not None else DEFAULT_PORT
    path = args.path if args.path is not None else env.get("NORA_MCP_PATH", DEFAULT_PATH)
    if args.stateless_http is not None:
        stateless_http = args.stateless_http
    else:
        env_val = env.get("NORA_MCP_STATELESS_HTTP")
        if env_val is None:
            stateless_http = DEFAULT_STATELESS_HTTP
        else:
            stateless_http = env_val.strip().lower() in {"1", "true", "yes", "on"}
    return TransportConfig(
        transport=transport,
        host=host,
        port=port,
        path=path,
        stateless_http=stateless_http,
    )


def _validate_transport(config: TransportConfig) -> None:
    """Raise `ValueError` on invalid transport or `sse`+`stateless_http=True`.

    Valid transport set comes from FastMCP 3.4.7's `run()` literal set;
    any other string would be rejected by FastMCP anyway — we surface it
    early with our own stderr message so the operator gets the valid
    option list before `mcp.run()` is invoked.

    `sse` + `stateless_http=True` is rejected by FastMCP at runtime
    (ValueError inside `run_http_async`); we pre-check so the boot
    fails with a NORA-formatted stderr message rather than the
    library's stack trace.
    """
    if config.transport not in VALID_TRANSPORTS:
        options = ", ".join(sorted(VALID_TRANSPORTS))
        raise ValueError(f"invalid transport {config.transport!r}; valid options: {options}")
    if config.transport == "sse" and config.stateless_http:
        raise ValueError(
            "transport 'sse' is incompatible with stateless_http=True "
            "(FastMCP requires stateful SSE)"
        )


def _exit_with_error(message: str) -> None:
    """Write `nora-mcp: <message>` to stderr and exit 2.

    Exit code 2 matches argparse's own usage-error exit code, so callers
    that already wire argparse into their shell pipeline get a familiar
    signal. NEVER invokes `mcp.run()` — the caller must guard that.
    """
    sys.stderr.write(f"nora-mcp: {message}\n")
    sys.exit(2)


def main(argv: Sequence[str] | None = None) -> None:
    """Boot NORA MCP: configure logging, load settings, wire driver, run MCP.

    `argv` is exposed so tests can drive the CLI in-process without a
    real subprocess. Defaults to `sys.argv[1:]` when None.
    """
    if argv is None:
        argv = sys.argv[1:]

    # Resolve + validate transport BEFORE any boot so a bad config never
    # touches the catalog verification, driver init, or filesystem.
    # `argparse` already raises SystemExit on its own usage errors; we
    # only need to catch our own `ValueError` for NORA-formatted errors.
    args = _parse_transport_args(argv)
    config = _resolve_transport_config(args)
    try:
        _validate_transport(config)
    except ValueError as exc:
        _exit_with_error(str(exc))
        return  # unreachable; `_exit_with_error` calls sys.exit(2).

    configure_logging()
    settings = Settings()
    # Boot the runtime state BEFORE the driver layer so the intervention
    # tools can read settings on their first invocation.
    set_runtime_state(settings)
    prompt_registry = PromptRegistry.from_settings(settings)
    set_prompt_registry(prompt_registry)
    catalog_registry = OidCatalogRegistry.verify_all(settings)
    inventory = Inventory.from_yaml(settings.nora_devices_inventory_path)
    set_driver(Pmp450iDriver(inventory=inventory, catalog_registry=catalog_registry))
    # PR 5 (slice 5): refuse any `@mcp.tool` whose name is not in
    # `OidCatalogRegistry.REQUIRED_OIDS_BY_TOOL[(vendor, model)]`. The
    # guard runs BEFORE `mcp.run()` so a rogue registration aborts the
    # boot — the tool is never exposed to MCP clients. The typed
    # `UncataloguedToolError` carries the offending tool name so the
    # operator can see which registration needs to be signed.
    verify_tools_are_catalogued(catalog_registry)
    register_tool_log_middleware()
    logger.info(
        "nora-mcp boot complete: catalogs=%s devices=%s transport=%s host=%s port=%s",
        settings.nora_oid_catalogs_path,
        len(inventory.device_ids),
        config.transport,
        config.host,
        config.port,
    )
    # Forward the resolved transport + kwargs to FastMCP. `show_banner=False`
    # keeps stderr clean of the FastMCP ASCII art + pypi.org update probe.
    # `cast` is safe: `_validate_transport` already raised on a bad value.
    #
    # FastMCP's `run_stdio_async()` only accepts `show_banner / log_level /
    # stateless`; passing `host / port / path / stateless_http` raises
    # `TypeError`. So we forward the network kwargs ONLY when the chosen
    # transport is HTTP-shaped (`http`, `streamable-http`, `sse`). For
    # stdio we pass just `show_banner=False` — exactly what the previous
    # `mcp.run(show_banner=False)` call site did.
    if config.transport == "stdio":
        mcp.run(
            transport=cast("TransportLiteral", config.transport),
            show_banner=False,
        )
    else:
        mcp.run(
            transport=cast("TransportLiteral", config.transport),
            host=config.host,
            port=config.port,
            path=config.path,
            stateless_http=config.stateless_http,
            show_banner=False,
        )


if __name__ == "__main__":
    main()
