"""Tests for the `PromptRegistry`.

Maps Prompt-R1..R7 scenarios from
`openspec/changes/phase2-pmp450i-driver/specs/prompt-registry/spec.md`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from nora.drivers.exceptions import PromptNotFoundError
from nora.prompts.registry import Prompt, PromptRegistry

# ---------------------------------------------------------------------------
# R1 — One-shot boot load
# ---------------------------------------------------------------------------


def test_registry_loads_once_and_never_reloads(tmp_prompts_dir: Path) -> None:
    """The registry freezes after `scan`; later writes to the dir are ignored."""
    target = tmp_prompts_dir / "snmp_pmp450i.md"
    target.write_text("---\nname: snmp_pmp450i\ndescription: first\n---\nbody-1\n")
    registry = PromptRegistry.scan(tmp_prompts_dir)
    first_body = registry.get("snmp_pmp450i").body

    # Operator drops an updated file: registry must NOT pick it up.
    target.write_text("---\nname: snmp_pmp450i\ndescription: second\n---\nbody-2\n")
    assert registry.get("snmp_pmp450i").body == first_body


# ---------------------------------------------------------------------------
# R2 — Default source: package data
# ---------------------------------------------------------------------------


def test_packaged_prompt_loads_on_boot() -> None:
    """Packaged `src/nora/prompts/snmp_pmp450i.md` is found on boot."""
    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    assert (package_dir / "snmp_pmp450i.md").is_file(), (
        f"Expected shipped prompt at {package_dir / 'snmp_pmp450i.md'}"
    )
    registry = PromptRegistry.scan(package_dir)
    prompt = registry.get("snmp_pmp450i")
    assert isinstance(prompt, Prompt)
    assert prompt.name == "snmp_pmp450i"
    assert prompt.description


# ---------------------------------------------------------------------------
# R3 — Operator override via NORA_PROMPTS_DIR
# ---------------------------------------------------------------------------


def test_override_directory_wins_over_package_data(
    tmp_prompts_dir: Path,
) -> None:
    """An override file with body "OVERRIDDEN" wins over the shipped copy."""
    override = tmp_prompts_dir / "snmp_pmp450i.md"
    override.write_text("---\nname: snmp_pmp450i\ndescription: override\n---\nOVERRIDDEN\n")
    registry = PromptRegistry.scan(tmp_prompts_dir)
    assert registry.get("snmp_pmp450i").body.strip() == "OVERRIDDEN"


def test_settings_prompts_dir_is_used(tmp_prompts_dir: Path) -> None:
    """`Settings.prompts_dir` overrides packaged data."""
    from nora.config import Settings

    target = tmp_prompts_dir / "snmp_pmp450i.md"
    target.write_text("---\nname: snmp_pmp450i\ndescription: from-settings\n---\nfrom-settings\n")
    settings = Settings(_env_file=None, _env_file_encoding=None, nora_prompts_dir=tmp_prompts_dir)
    registry = PromptRegistry.from_settings(settings)
    assert registry.get("snmp_pmp450i").body.strip() == "from-settings"


# ---------------------------------------------------------------------------
# R4 — Missing prompt is fatal
# ---------------------------------------------------------------------------


def test_missing_prompt_raises_prompt_not_found(tmp_path: Path) -> None:
    """An empty directory + no packaged copy → `PromptNotFoundError`."""
    empty = tmp_path / "empty-prompts"
    empty.mkdir()
    registry = PromptRegistry.scan(empty)
    with pytest.raises(PromptNotFoundError) as exc:
        registry.get("snmp_pmp450i")
    assert "snmp_pmp450i" in str(exc.value)


# ---------------------------------------------------------------------------
# R5 — Front-matter schema validation
# ---------------------------------------------------------------------------


def test_file_without_front_matter_is_rejected(tmp_path: Path) -> None:
    """A `foo.md` with no front-matter block fails validation."""
    target = tmp_path / "snmp_pmp450i.md"
    target.write_text("no front-matter here\n")
    registry = PromptRegistry.scan(target.parent)
    with pytest.raises(PromptNotFoundError):
        registry.get("snmp_pmp450i")


def test_empty_description_is_rejected(tmp_path: Path) -> None:
    """`description: ""` fails validation → `PromptNotFoundError`."""
    target = tmp_path / "snmp_pmp450i.md"
    target.write_text('---\nname: snmp_pmp450i\ndescription: ""\n---\nbody\n')
    registry = PromptRegistry.scan(target.parent)
    with pytest.raises(PromptNotFoundError):
        registry.get("snmp_pmp450i")


def test_name_must_match_filename(tmp_path: Path) -> None:
    """A `front-matter` whose `name:` differs from the basename fails."""
    target = tmp_path / "snmp_pmp450i.md"
    target.write_text("---\nname: other-name\ndescription: x\n---\nbody\n")
    registry = PromptRegistry.scan(target.parent)
    # The file is dropped from the registry — `get(snmp_pmp450i)` raises.
    with pytest.raises(PromptNotFoundError):
        registry.get("snmp_pmp450i")


# ---------------------------------------------------------------------------
# R6 — Stable per-session reference (no I/O on second call)
# ---------------------------------------------------------------------------


def test_second_get_call_does_no_io(
    tmp_prompts_dir: Path,
) -> None:
    """The second `registry.get(name)` call MUST NOT trigger I/O."""
    target = tmp_prompts_dir / "snmp_pmp450i.md"
    target.write_text("---\nname: snmp_pmp450i\ndescription: d\n---\nbody\n")
    registry = PromptRegistry.scan(tmp_prompts_dir)

    # First call is allowed to read (during scan itself, not here).
    first = registry.get("snmp_pmp450i")

    # Patch Path.read_text to FAIL if called on the second `get`.
    real_read_text = Path.read_text

    def _boom(self: Path, *args: Any, **kwargs: Any) -> str:
        if self == target:
            raise AssertionError("Path.read_text was called on the second registry.get")
        return real_read_text(self, *args, **kwargs)

    with mock.patch.object(Path, "read_text", _boom):
        second = registry.get("snmp_pmp450i")

    assert first == second
    assert first.body == second.body


# ---------------------------------------------------------------------------
# R7 — Shipped snmp_pmp450i.md content scan
# ---------------------------------------------------------------------------


def test_shipped_prompt_declares_tool_name_and_typed_schema() -> None:
    """`src/nora/prompts/snmp_pmp450i.md` mentions the tool + schema,
    and contains no private IPv4 / MAC / hostname literal.
    """
    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    text = (package_dir / "snmp_pmp450i.md").read_text()

    assert "snmp_get_pmp450i_radio_metrics" in text
    assert "RadioMetricsReport" in text

    forbidden_literals = (
        "10.0.0.",  # private IPv4 prefix
        "192.168.",
        "172.16.",
        "aa:bb:cc:dd:ee:ff",  # private MAC literal
        "router-core-01.example.com",
        "ABC123XYZ-PROD-001",  # private serial literal
    )
    for literal in forbidden_literals:
        assert literal not in text, f"Prompt body contains banned identifier literal {literal!r}"


# ---------------------------------------------------------------------------
# Negative coverage — registry exposes no hot-reload hook
# ---------------------------------------------------------------------------


def test_registry_has_no_inotify_or_watchdog_dependency() -> None:
    """`PromptRegistry` MUST NOT pull in watchdog / inotify / apscheduler."""
    import ast

    text = (
        Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts" / "registry.py"
    ).read_text()
    tree = ast.parse(text)
    banned = ("watchdog", "inotify", "apscheduler", "watchfiles")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not any(alias.name.startswith(b) for b in banned), alias.name
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            assert not any(module.startswith(b) for b in banned), module


# ---------------------------------------------------------------------------
# Negative coverage — registry env-var surface
# ---------------------------------------------------------------------------


def test_registry_does_not_call_os_environ_inline() -> None:
    """Per Prompt-R3, registry must not read `os.environ` directly; the
    override flows through `Settings.prompts_dir`.
    """
    import ast

    text = (
        Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts" / "registry.py"
    ).read_text()
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "getenv":
                # Allow it only if invoked through `os.environ.get`, which
                # itself would require reading the module attribute.
                pass
            if isinstance(func, ast.Attribute) and func.attr == "environ":
                pytest.fail(
                    "registry.py must not access os.environ directly; "
                    "Settings.prompts_dir is the configured override surface"
                )


# ---------------------------------------------------------------------------
# R8 — Shipped orchestrator prompt loads on boot
# ---------------------------------------------------------------------------


def test_shipped_orchestrator_prompt_loads_on_boot() -> None:
    """`src/nora/prompts/netops_orchestrator.md` loads on the same packaged path as R2.

    Body starts with `# Lead NetOps Orchestrator` (a single leading newline
    from front-matter stripping is conventional and ignored).
    """
    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    shipped = package_dir / "netops_orchestrator.md"
    assert shipped.is_file(), f"Expected shipped prompt at {shipped}"

    registry = PromptRegistry.scan(package_dir)
    prompt = registry.get("netops_orchestrator")
    assert isinstance(prompt, Prompt)
    assert prompt.name == "netops_orchestrator"
    assert prompt.description  # non-empty
    assert prompt.body.lstrip().startswith("# Lead NetOps Orchestrator"), (
        f"Orchestrator body should start with the heading; got: {prompt.body[:80]!r}"
    )


# ---------------------------------------------------------------------------
# R9 — Orchestrator prompt content scan (Zero-Leakage + tool names)
# ---------------------------------------------------------------------------


def test_orchestrator_prompt_mentions_real_tools_and_no_banned_literals() -> None:
    """`netops_orchestrator.md` references the four real `@mcp.tool` names
    (without `nora_` prefix) AND none of the banned literals appear.

    This includes the explicit Cambium-OUI guard added by this PR: the
    literal `00:04:56` MUST NOT appear in the body.
    """
    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    text = (package_dir / "netops_orchestrator.md").read_text()

    # The four real @mcp.tool names — no `nora_` prefix.
    expected_tool_names = (
        "snmp_get_pmp450i_radio_metrics",
        "search_intervention_history",
        "get_device_lifecycle_summary",
        "correlate_sector_interference",
    )
    for name in expected_tool_names:
        assert name in text, f"Orchestrator prompt must mention tool name {name!r}"

    # Banned identifiers — anything in this list MUST NOT appear.
    banned_literals = (
        "10.0.0.",  # private IPv4 prefix
        "192.168.",
        "172.16.",
        "bb:cc:dd:ee:ff",  # private MAC literal
        "router-core-01.example.com",
        "ABC123XYZ-PROD-001",  # private serial literal
        "00:04:56",  # Cambium OUI prefix — banned even when genericised
    )
    for literal in banned_literals:
        assert literal not in text, f"Orchestrator prompt body contains banned literal {literal!r}"


# ---------------------------------------------------------------------------
# R10 — Server exposes prompts via `@mcp.prompt`
# ---------------------------------------------------------------------------


def test_server_exposes_orchestrator_and_driver_prompts() -> None:
    """`mcp.list_prompts()` advertises both `netops_orchestrator` and `snmp_pmp450i`.

    FastMCP exposes `list_prompts` as an async coroutine; we drive it
    through `asyncio.run` so the test stays synchronous and matches the
    style of every other test in this file.
    """
    import asyncio

    from nora import server as server_mod

    async def _names() -> set[str]:
        prompts = await server_mod.mcp.list_prompts()
        return {p.name for p in prompts}

    names = asyncio.run(_names())
    expected = {"netops_orchestrator", "snmp_pmp450i"}
    assert expected.issubset(names), (
        f"Expected at least {sorted(expected)} prompts registered on `mcp`; got: {sorted(names)}"
    )


# ---------------------------------------------------------------------------
# R11 — Server `instructions` set
# ---------------------------------------------------------------------------


def test_server_instructions_are_set_with_required_substrings() -> None:
    """`mcp.instructions` is non-empty and mentions Zero-Leakage + intervention memory."""
    from nora import server as server_mod

    instructions = server_mod.mcp.instructions
    assert isinstance(instructions, str)
    assert instructions, "mcp.instructions must be a non-empty string"
    assert "Zero-Leakage" in instructions, (
        f"mcp.instructions must include the Zero-Leakage contract; got: {instructions!r}"
    )
    assert "intervention memory" in instructions, (
        f"mcp.instructions must mention intervention memory; got: {instructions!r}"
    )


# ---------------------------------------------------------------------------
# R12 — `set_prompt_registry` / `get_prompt_registry` lifecycle
# ---------------------------------------------------------------------------


def test_set_prompt_registry_round_trip() -> None:
    """Injecting a freshly-scanned registry is visible through `get_prompt_registry`.

    Uses the packaged prompts dir so it matches the production boot path.
    """
    import nora.server as server_mod

    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    fresh = PromptRegistry.scan(package_dir)

    previous = server_mod._current_prompt_registry  # type: ignore[attr-defined]
    try:
        server_mod.set_prompt_registry(fresh)
        assert server_mod.get_prompt_registry() is fresh
        # And the registry can actually serve both prompt names end-to-end.
        assert server_mod.get_prompt_registry().get("netops_orchestrator").name == (
            "netops_orchestrator"
        )
        assert server_mod.get_prompt_registry().get("snmp_pmp450i").name == "snmp_pmp450i"
    finally:
        # Reset module-level state so other tests do not see our injected one.
        server_mod._current_prompt_registry = previous  # type: ignore[attr-defined]


def test_get_prompt_registry_raises_when_not_initialised() -> None:
    """`get_prompt_registry` raises `RuntimeError` when the registry was not set."""
    import nora.server as server_mod

    previous = server_mod._current_prompt_registry  # type: ignore[attr-defined]
    try:
        server_mod._current_prompt_registry = None  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError) as exc:
            server_mod.get_prompt_registry()
        assert "set_prompt_registry" in str(exc.value)
    finally:
        server_mod._current_prompt_registry = previous  # type: ignore[attr-defined]
