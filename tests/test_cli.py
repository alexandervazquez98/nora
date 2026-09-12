"""Tests for the `nora.cli` canonical entry point.

The thin MCP boot sequence lives here:
    Settings() -> set_runtime_state -> PromptRegistry.from_settings ->
    OidCatalogRegistry.verify_all -> Inventory.from_yaml ->
    set_driver(Pmp450iDriver(...)) -> register_tool_log_middleware ->
    mcp.run(show_banner=False)
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path


def test_cli_module_exists_and_exports_main() -> None:
    """`src/nora/cli.py` MUST exist and expose a callable `main`."""
    from nora import cli

    assert callable(getattr(cli, "main", None)), "nora.cli must export a callable `main`"


def test_cli_main_has_correct_boot_sequence() -> None:
    """`cli.main()` MUST wire Settings -> set_runtime_state -> ... -> mcp.run().

    Verifies the boot sequence textually (covers future regressions).
    """
    from nora import cli

    src = inspect.getsource(cli.main)
    assert "Settings()" in src, "cli.main must construct Settings()"
    assert "set_runtime_state(" in src, "cli.main must call set_runtime_state"
    assert "PromptRegistry" in src, "cli.main must construct the PromptRegistry"
    assert "OidCatalogRegistry.verify_all" in src, "cli.main must verify the OID catalogs at boot"
    assert "Inventory.from_yaml" in src, "cli.main must load the YAML inventory"
    assert "set_driver(" in src, "cli.main must inject the driver singleton"
    assert "register_tool_log_middleware" in src, "cli.main must register the tool-log middleware"
    assert "mcp.run(" in src, "cli.main must invoke mcp.run() to start the FastMCP server"
    # Boot MUST NOT construct LLMProvider / init_session_journal / build_provider.
    for forbidden in ("build_provider", "init_session_journal", "_AutoTraceMiddleware"):
        assert forbidden not in src, (
            f"cli.main must NOT reference {forbidden!r} after the thin split; got: {src!r}"
        )


def test_cli_main_invokes_mcp_run_without_transport_arg() -> None:
    """`mcp.run(show_banner=False)` MUST default to stdio (no transport arg)."""
    from nora import cli

    src = inspect.getsource(cli.main)
    # The exact call must be `mcp.run(show_banner=False)` — no `transport=` kwarg.
    assert "mcp.run(show_banner=False)" in src, (
        f"cli.main must call mcp.run(show_banner=False); got: {src!r}"
    )
    assert "transport=" not in src, (
        f"cli.main must NOT pass transport= (defaults to stdio); got: {src!r}"
    )


def test_cli_py_sets_fastermcp_banner_env_default() -> None:
    """`cli.py` MUST set `FASTMCP_SHOW_SERVER_BANNER=false` BEFORE importing fastmcp."""
    from nora import cli

    src = inspect.getsource(cli)
    # The setdefault call must appear above the first `from nora.config`
    # (or any other) import — we check that the env var is set.
    assert "FASTMCP_SHOW_SERVER_BANNER" in src, (
        f"cli.py must set FASTMCP_SHOW_SERVER_BANNER; got: {src!r}"
    )


def test_cli_py_does_not_import_llm_or_journal() -> None:
    """AST guard: `cli.py` MUST NOT import from `nora.llm` or `nora.core.session_*`."""
    from nora import cli

    cli_path = Path(cli.__file__)
    src = cli_path.read_text()
    tree = ast.parse(src)
    banned = (
        "nora.llm",
        "nora.core.session_journal",
        "nora.core.session_models",
        "nora.core.session_paths",
        "nora.core.session_redaction",
        "nora.core.session_rotation",
    )
    offenders: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name == b or alias.name.startswith(b + ".") for b in banned):
                    offenders.append((node.lineno, "import", alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            if any(node.module == b or node.module.startswith(b + ".") for b in banned):
                offenders.append((node.lineno, "importfrom", node.module))
    assert offenders == [], f"cli.py must not import from LLM/journal; got: {offenders}"
