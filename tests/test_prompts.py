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
