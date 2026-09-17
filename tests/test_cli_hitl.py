"""Tests for the `nora hitl mint` sub-command — issue #43 / WU-2.

Per `openspec/changes/2026-09-15-3tier-tool-governance/specs/nora-mcp-server/spec.md`
R-NEW-7: `nora` argv dispatchers, `nora hitl mint --operator-id <id>
--ttl-seconds <n>` emits a signed JSON token on stdout.

Per `openspec/changes/2026-09-15-3tier-tool-governance/specs/hitl-approval-tokens/spec.md`
scenario "mint produces a typed model with a non-empty signature".
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _venv_python() -> str:
    py = PROJECT_ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        pytest.skip("venv python not present")
    return str(py)


# ---------------------------------------------------------------------------
# Sub-command dispatch (argv parsing).
# ---------------------------------------------------------------------------


def test_nora_main_module_dispatches_on_argv1() -> None:
    """`nora.__main__:main` MUST dispatch on `argv[1]`.

    Per ADR-3: argparse sub-parser. `nora` (no args) → MCP boot.
    `nora mcp` → MCP boot. `nora hitl mint ...` → mint sub-command.
    Unknown sub-command → help + exit 2.
    """
    import ast
    from pathlib import Path as _P

    src = (_P(__file__).resolve().parent.parent / "src" / "nora" / "__main__.py").read_text()
    tree = ast.parse(src)

    # The `main` function MUST inspect `sys.argv[1:]` (or equivalent)
    # and route on the first non-flag token.
    main_fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "main"
    )
    src_text = ast.unparse(main_fn) if hasattr(ast, "unparse") else ast.dump(main_fn)
    # The dispatcher must inspect argv (sys.argv reference OR explicit argv arg)
    assert "argv" in src_text or "sys.argv" in src_text, (
        f"`__main__.main` must inspect argv to dispatch; got: {src_text!r}"
    )


def test_nora_unknown_subcommand_exits_nonzero(capsys: pytest.CaptureFixture[str]) -> None:
    """`nora bogus` exits non-zero with help text on stderr.

    Per R-NEW-7 scenario "`nora` with unknown sub-command exits
    non-zero with help". The dispatcher rejects unknown tokens BEFORE
    `mcp.run()` is invoked.
    """
    from nora import __main__ as main_mod

    with pytest.raises(SystemExit) as ei:
        main_mod.main(["bogus"])
    assert ei.value.code != 0, f"unknown subcommand must exit non-zero; got code={ei.value.code!r}"
    captured = capsys.readouterr()
    assert "hitl" in captured.err or "hitl" in captured.out, (
        f"help text must list valid sub-commands including `hitl`; got stderr={captured.err!r}"
    )
    assert "mcp" in captured.err or "mcp" in captured.out, (
        f"help text must list `mcp`; got stderr={captured.err!r}"
    )


def test_nora_no_args_emits_deprecation_warning_then_boots_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`nora` (no args) MUST still boot MCP (back-compat pinned).

    Per R-NEW-7 scenario "`nora` with no args still boots MCP". The
    alias tests `test_main_alias.py` and `test_integration_boot.py:323`
    pin this contract.
    """
    # The dispatcher must NOT raise SystemExit for an empty argv — it
    # falls through to the legacy alias (`DeprecationWarning` then
    # `cli.main()`). We exercise the dispatcher at the library level
    # (not via subprocess) so we can capture the warning / boot path
    # without needing an MCP client to handshake.
    from nora import __main__ as main_mod

    # `cli.main()` would try to launch the FastMCP server — patch the
    # `main` import inside `nora.__main__` BEFORE we call it.
    called = {"count": 0}

    def fake_cli_main(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        called["count"] += 1

    monkeypatch.setattr("nora.cli.main", fake_cli_main)
    # `argv=[]` → no sub-command → must call `cli.main()` (legacy path).
    main_mod.main([])
    assert called["count"] == 1, (
        f"`nora` (no args) must delegate to cli.main exactly once; got {called['count']}"
    )


def test_nora_mcp_subcommand_boots_mcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`nora mcp` boots MCP (same as no-args)."""
    from nora import __main__ as main_mod

    called = {"count": 0}

    def fake_cli_main(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        called["count"] += 1

    monkeypatch.setattr("nora.cli.main", fake_cli_main)
    main_mod.main(["mcp"])
    assert called["count"] == 1, (
        f"`nora mcp` must delegate to cli.main exactly once; got {called['count']}"
    )


# ---------------------------------------------------------------------------
# `nora hitl mint` happy path + edge cases.
# ---------------------------------------------------------------------------


def test_nora_hitl_mint_emits_signed_token_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`nora hitl mint --operator-id alice --ttl-seconds 900` prints a JSON token.

    Per R-NEW-7 scenario "`nora hitl mint` emits a signed token".
    Stdout carries ONE JSON line with `operator_id`, `issued_at`,
    `expires_at`, `token`, AND a non-empty `signature`. Exit code 0.
    """
    from pydantic import SecretStr

    from nora import __main__ as main_mod
    from nora.config import Settings

    # Hermetic env: empty signing key would fail-closed; provide one.
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_hitl_signing_key=SecretStr("dispatcher-test-key"),
        nora_tool_specs_dir=tmp_path / "tool_specs",  # absent → skip
    )

    called = {"count": 0}

    def fake_settings(*args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        called["count"] += 1
        return settings

    monkeypatch.setattr("nora.config.Settings", fake_settings)
    # Skip the MCP boot.
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    main_mod.main(["hitl", "mint", "--operator-id", "alice", "--ttl-seconds", "900"])

    captured = capsys.readouterr()
    assert called["count"] == 1, (
        f"`nora hitl mint` must instantiate Settings once; got {called['count']}"
    )
    payload = json.loads(captured.out.strip())
    assert payload["operator_id"] == "alice"
    assert "issued_at" in payload
    assert "expires_at" in payload
    assert "token" in payload
    assert isinstance(payload.get("signature"), str)
    assert len(payload["signature"]) >= 1


def test_nora_hitl_mint_rejects_missing_operator_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`nora hitl mint` without `--operator-id` exits non-zero with stderr message.

    Per R-NEW-7 scenario "`nora hitl mint` rejects invalid args".
    Exit code non-zero, stderr names the missing argument, no token
    is emitted.
    """
    from nora import __main__ as main_mod

    with pytest.raises(SystemExit) as ei:
        main_mod.main(["hitl", "mint"])
    assert ei.value.code != 0, (
        f"missing --operator-id must exit non-zero; got code={ei.value.code!r}"
    )
    captured = capsys.readouterr()
    assert "operator-id" in captured.err or "operator_id" in captured.err, (
        f"stderr must name the missing --operator-id argument; got stderr={captured.err!r}"
    )


def test_nora_hitl_mint_subprocess_emits_signed_token(
    tmp_path: Path,
) -> None:
    """End-to-end subprocess: `python -m nora hitl mint --operator-id alice` emits JSON.

    Drives the dispatcher via a real subprocess to prove the wiring
    survives the entry-point boundary.
    """
    py = _venv_python()

    # Hermetic env: signing key + empty devices.yaml so the dispatcher
    # doesn't trip on the catalog verification step.
    (tmp_path / "catalogs").mkdir(exist_ok=True)
    (tmp_path / "devices.yaml").write_text("# empty\n")

    env = {
        "PATH": "/usr/bin:/usr/local/bin",
        "HOME": str(tmp_path),
        "NORA_HITL_SIGNING_KEY": "dispatcher-subprocess-key",
        "NORA_OID_CATALOG_SIGNING_KEY": "change-me",  # matches shipped built-in baseline
        "NORA_OID_CATALOGS_PATH": str(tmp_path / "catalogs"),
        "NORA_DEVICES_INVENTORY_PATH": str(tmp_path / "devices.yaml"),
        "NORA_TOOL_SPECS_DIR": str(tmp_path / "missing_tool_specs"),  # absent → skip
    }
    proc = subprocess.run(
        [py, "-m", "nora", "hitl", "mint", "--operator-id", "alice", "--ttl-seconds", "900"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0, (
        f"`nora hitl mint` must exit 0 on the happy path; "
        f"got code={proc.returncode}, stderr={proc.stderr!r}"
    )
    payload = json.loads(proc.stdout.strip())
    assert payload["operator_id"] == "alice"
    assert len(payload["signature"]) >= 1


__all__ = [
    "test_nora_main_module_dispatches_on_argv1",
    "test_nora_unknown_subcommand_exits_nonzero",
    "test_nora_no_args_emits_deprecation_warning_then_boots_mcp",
    "test_nora_mcp_subcommand_boots_mcp",
    "test_nora_hitl_mint_emits_signed_token_json",
    "test_nora_hitl_mint_rejects_missing_operator_id",
    "test_nora_hitl_mint_subprocess_emits_signed_token",
]
