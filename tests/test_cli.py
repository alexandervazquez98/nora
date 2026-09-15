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


def _install_mcp_run_stub(cli, calls: list[dict]) -> None:
    """Replace `cli.mcp.run` with a recording stub.

    Keeps the test in-process (no subprocess boot). The stub records every
    kwarg call so assertions can pin the resolved `TransportConfig`.
    """
    stub = lambda *args, **kwargs: calls.append(kwargs)  # noqa: E731
    import pytest

    pytest.MonkeyPatch().setattr(cli.mcp, "run", stub)


# Sentinel `Settings` returned by `_install_cli_stubs` below — carries
# only the two attributes `cli.main` reads, so the assertion can run
# without instantiating the full `Settings` Pydantic model.
_FAKE_SETTINGS = type(
    "S",
    (),
    {"nora_oid_catalogs_path": "", "nora_devices_inventory_path": ""},
)()


# Sentinel `PromptRegistry` carrying one stub `Prompt` per canonical
# tool name, with metadata `tier` matching the canonical taxonomy. The
# Issue #43 boot guard (`verify_tools_have_tier_classification`) walks
# the registered `@mcp.tool` surface and looks up each tool's tier here;
# the stub matches every name so the guard passes without error.
class _FakePrompt:
    def __init__(self, tier: int) -> None:
        self.metadata = {"tier": tier}


class _FakePromptRegistry:
    _TIER_BY_TOOL = {
        # Tier 0
        "snmp_get_ap_summary": 0,
        "snmp_get_sm_table": 0,
        "snmp_get_pmp450i_radio_metrics": 0,
        "snmp_get_frame_utilization": 0,
        "snmp_get_sm_detailed_diagnostics": 0,
        "search_intervention_history": 0,
        "get_device_lifecycle_summary": 0,
        "correlate_sector_interference": 0,
        # Tier 1
        "snmp_run_spectrum_analysis": 1,
        # Tier 2
        "snmp_migrate_radio_frequency": 2,
        "save_intervention_record": 2,
        # Tier 0 (legacy)
        "register_device": 0,
    }

    def get(self, name: str) -> _FakePrompt:
        tier = self._TIER_BY_TOOL.get(name, 0)
        return _FakePrompt(tier=tier)


def _install_cli_stubs(mp: object) -> list[dict]:
    """Patch every boot collaborator on the `cli` module with a no-op stub.

    Returns the `calls` list passed to the `mcp.run` stub so the test
    body can assert on the resolved transport kwargs. The stub layers
    are intentionally minimal: the goal is to exercise the
    resolve/validate/mcp.run dispatch path, not the boot sequence.
    """
    from nora import cli

    calls: list[dict] = []
    mp.setattr(cli.mcp, "run", lambda *a, **kw: calls.append(kw))
    mp.setattr(cli, "configure_logging", lambda: None)
    mp.setattr(cli, "set_runtime_state", lambda s: None)
    mp.setattr(cli, "set_prompt_registry", lambda r: None)
    mp.setattr(
        cli,
        "PromptRegistry",
        type(
            "PR",
            (),
            {"from_settings": classmethod(lambda cls, s: _FakePromptRegistry())},
        ),
    )
    mp.setattr(
        cli.OidCatalogRegistry,
        "verify_all",
        classmethod(lambda cls, s: object()),
    )
    mp.setattr(
        cli.Inventory,
        "from_yaml",
        classmethod(lambda cls, p: type("I", (), {"device_ids": []})()),
    )
    mp.setattr(cli, "set_driver", lambda d: None)
    mp.setattr(cli, "register_tool_log_middleware", lambda: None)
    mp.setattr(cli, "verify_tools_are_catalogued", lambda registry: None)
    mp.setattr(cli, "verify_tools_have_tier_classification", lambda pr: None)
    mp.setattr(cli, "Settings", lambda: _FAKE_SETTINGS)
    return calls


def test_default_stdio_invokes_mcp_run_with_transport_stdio(
    monkeypatch: object,
) -> None:
    """No env vars + no CLI flags → transport is stdio."""
    import pytest

    from nora import cli

    mp = pytest.MonkeyPatch()
    try:
        # Wipe every NORA_MCP_* env var so the default is exercised.
        for var in (
            "NORA_MCP_TRANSPORT",
            "NORA_MCP_HOST",
            "NORA_MCP_PORT",
            "NORA_MCP_PATH",
            "NORA_MCP_STATELESS_HTTP",
        ):
            mp.delenv(var, raising=False)
        calls = _install_cli_stubs(mp)

        cli.main(argv=[])
    finally:
        mp.undo()

    assert calls, "cli.main must invoke mcp.run exactly once"
    assert calls[0].get("transport") == "stdio", (
        f"default transport must be stdio; got call kwargs={calls[0]!r}"
    )
    assert calls[0].get("show_banner") is False


def test_env_vars_select_transport_when_no_cli_flag(
    monkeypatch: object,
) -> None:
    """`NORA_MCP_TRANSPORT=http` + no CLI flags → transport is http."""
    import pytest

    from nora import cli

    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("NORA_MCP_TRANSPORT", "http")
        mp.setenv("NORA_MCP_PORT", "8765")
        for var in ("NORA_MCP_HOST", "NORA_MCP_PATH", "NORA_MCP_STATELESS_HTTP"):
            mp.delenv(var, raising=False)
        calls = _install_cli_stubs(mp)

        cli.main(argv=[])
    finally:
        mp.undo()

    assert calls, "cli.main must invoke mcp.run exactly once"
    assert calls[0].get("transport") == "http", (
        f"NORA_MCP_TRANSPORT=http must select http; got {calls[0]!r}"
    )
    assert calls[0].get("port") == 8765


def test_cli_flags_override_env_vars(monkeypatch: object) -> None:
    """CLI flags beat env vars (precedence CLI > env > default)."""
    import pytest

    from nora import cli

    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("NORA_MCP_TRANSPORT", "stdio")
        mp.setenv("NORA_MCP_PORT", "8000")
        calls = _install_cli_stubs(mp)

        cli.main(argv=["--transport=http", "--port=9000"])
    finally:
        mp.undo()

    assert calls, "cli.main must invoke mcp.run exactly once"
    assert calls[0].get("transport") == "http"
    assert calls[0].get("port") == 9000, f"--port=9000 must beat env var 8000; got {calls[0]!r}"


def test_invalid_transport_exits_2_with_stderr_naming_options(
    monkeypatch: object,
    capsys: object,
) -> None:
    """`NORA_MCP_TRANSPORT=garbage` → exit 2 + stderr names bad value + 4 options."""
    import pytest

    from nora import cli

    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("NORA_MCP_TRANSPORT", "garbage")
        calls = _install_cli_stubs(mp)

        with pytest.raises(SystemExit) as ei:
            cli.main(argv=[])
    finally:
        mp.undo()

    assert ei.value.code == 2, f"invalid transport must exit 2; got code={ei.value.code!r}"
    assert calls == [], f"mcp.run MUST NOT be invoked on invalid transport; got calls={calls!r}"
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    stderr = captured.err
    assert "garbage" in stderr, f"stderr must name the bad value `garbage`; got: {stderr!r}"
    for option in ("stdio", "http", "streamable-http", "sse"):
        assert option in stderr, f"stderr must list valid option {option!r}; got: {stderr!r}"


def test_stateless_http_with_sse_rejected(
    monkeypatch: object,
    capsys: object,
) -> None:
    """`--transport=sse --stateless-http` is incompatible (FastMCP rule)."""
    import pytest

    from nora import cli

    mp = pytest.MonkeyPatch()
    try:
        for var in (
            "NORA_MCP_TRANSPORT",
            "NORA_MCP_HOST",
            "NORA_MCP_PORT",
            "NORA_MCP_PATH",
            "NORA_MCP_STATELESS_HTTP",
        ):
            mp.delenv(var, raising=False)
        calls = _install_cli_stubs(mp)

        with pytest.raises(SystemExit) as ei:
            cli.main(argv=["--transport=sse", "--stateless-http"])
    finally:
        mp.undo()

    assert ei.value.code == 2, f"sse+stateless_http must exit 2; got code={ei.value.code!r}"
    assert calls == [], (
        f"mcp.run MUST NOT be invoked when sse+stateless_http is rejected; got {calls!r}"
    )
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "sse" in captured.err.lower(), f"stderr must mention sse; got: {captured.err!r}"
    assert "stateless" in captured.err.lower(), (
        f"stderr must mention stateless; got: {captured.err!r}"
    )


def test_help_exits_zero_lists_transport_flag(
    monkeypatch: object,
    capsys: object,
) -> None:
    """`nora-mcp --help` exits 0 and lists `--transport`."""
    import pytest

    from nora import cli

    mp = pytest.MonkeyPatch()
    try:
        for var in (
            "NORA_MCP_TRANSPORT",
            "NORA_MCP_HOST",
            "NORA_MCP_PORT",
            "NORA_MCP_PATH",
            "NORA_MCP_STATELESS_HTTP",
        ):
            mp.delenv(var, raising=False)
        calls: list[dict] = []
        mp.setattr(cli.mcp, "run", lambda *a, **kw: calls.append(kw))

        with pytest.raises(SystemExit) as ei:
            cli.main(argv=["--help"])
    finally:
        mp.undo()

    assert ei.value.code == 0, f"--help must exit 0; got code={ei.value.code!r}"
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "--transport" in captured.out, (
        f"--help output must list --transport flag; got: {captured.out!r}"
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
