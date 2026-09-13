"""Canonical NORA MCP entry point.

Wires the runtime together and hands control to FastMCP. Invoked via
the `nora-mcp` console script (see `[project.scripts]` in
`pyproject.toml`). `python -m nora` is a deprecated alias that
delegates to this module.

Boot sequence:

    Settings() -> set_runtime_state(settings) ->
    PromptRegistry.from_settings -> OidCatalogRegistry.verify_all ->
    Inventory.from_yaml -> set_driver(Pmp450iDriver(...)) ->
    register_tool_log_middleware -> mcp.run(show_banner=False)

Default transport is stdio (per FastMCP `mcp.run()` with no transport
arg), so this entry point is what Claude Desktop and other JSON-RPC
clients connect to over the local subprocess.
"""

from __future__ import annotations

import logging
import os

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
)

logger = logging.getLogger("nora.cli")


def main() -> None:
    """Boot NORA MCP: configure logging, load settings, wire driver, run MCP."""
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
    register_tool_log_middleware()
    logger.info(
        "nora-mcp boot complete: catalogs=%s devices=%s",
        settings.nora_oid_catalogs_path,
        len(inventory.device_ids),
    )
    # `mcp.run()` with no transport argument defaults to stdio, which is
    # the JSON-RPC transport every MCP client (Claude Desktop, inspector,
    # etc.) speaks over a local subprocess. `show_banner=False` keeps the
    # startup stderr clean of the FastMCP ASCII art and pypi.org update
    # probe.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
