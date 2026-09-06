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
from nora.llm import build_provider  # noqa: E402
from nora.server import configure_logging, mcp, set_runtime_state  # noqa: E402

logger = logging.getLogger("nora.main")


def main() -> None:
    """Boot NORA: configure logging, load settings, build provider, run MCP."""
    configure_logging()
    settings = Settings()
    provider = build_provider(settings)
    set_runtime_state(settings, provider)
    logger.info(
        "nora boot complete: active_provider=%s env_loaded=%s",
        settings.nora_llm_provider,
        settings.loaded_from == ".env",
    )
    # `mcp.run()` with no transport argument defaults to stdio, which is the
    # JSON-RPC transport every MCP client (Claude Desktop, inspector, etc.)
    # speaks over a local subprocess. `show_banner=False` keeps the startup
    # stderr clean of the FastMCP ASCII art and pypi.org update probe.
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
