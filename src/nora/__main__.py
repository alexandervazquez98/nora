"""NORA entry point.

Wires the runtime together and hands control to FastMCP. Invoked via
`python -m nora`; the `[project.scripts]` entry in `pyproject.toml` exposes
the same callable as the `nora` console script.

Default transport is stdio (per FastMCP `mcp.run()` with no transport arg),
so this entry point is what Claude Desktop and other JSON-RPC clients connect
to over the local subprocess.
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
from nora.llm import build_provider  # noqa: E402
from nora.prompts.registry import PromptRegistry  # noqa: E402
from nora.server import (  # noqa: E402
    configure_logging,
    init_session_journal,
    mcp,
    register_auto_trace_middleware,
    set_runtime_state,
)

logger = logging.getLogger("nora.main")


def main() -> None:
    """Boot NORA: configure logging, load settings, build provider, run MCP."""
    configure_logging()
    settings = Settings()
    provider = build_provider(settings)
    set_runtime_state(settings, provider)
    # Phase 2 — initialise the session journal singleton and mount the
    # auto-trace middleware AFTER the runtime state is in place. The
    # middleware reads `get_journal()` lazily on every tool call.
    init_session_journal(settings)
    # Phase 2 driver layer — load prompts once, verify every OID catalog
    # under `settings.nora_oid_catalogs_path`, build the inventory, and
    # inject the driver singleton. Both `PromptRegistry.scan` and
    # `OidCatalogRegistry.verify_all` are boot-fatal on failure.
    PromptRegistry.from_settings(settings)
    catalog_registry = OidCatalogRegistry.verify_all(settings)
    inventory = Inventory.from_yaml(settings.nora_devices_inventory_path)
    set_driver(Pmp450iDriver(inventory=inventory, catalog_registry=catalog_registry))
    register_auto_trace_middleware()
    logger.info(
        "nora boot complete: active_provider=%s env_loaded=%s journal_dir=%s "
        "catalogs=%s devices=%s",
        settings.nora_llm_provider,
        settings.loaded_from == ".env",
        settings.nora_session_journal_dir,
        settings.nora_oid_catalogs_path,
        len(inventory.device_ids),
    )
    # `mcp.run()` with no transport argument defaults to stdio, which is the
    # JSON-RPC transport every MCP client (Claude Desktop, inspector, etc.)
    # speaks over a local subprocess. `show_banner=False` keeps the startup
    # stderr clean of the FastMCP ASCII art and pypi.org update probe.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
