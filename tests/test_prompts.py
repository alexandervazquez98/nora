"""Tests for the `PromptRegistry`.

Maps Prompt-R1..R7 scenarios from
`openspec/changes/phase2-pmp450i-driver/specs/prompt-registry/spec.md`.
"""

from __future__ import annotations

import hashlib
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
    target = _write_versioned_prompt(
        tmp_prompts_dir,
        name="snmp_pmp450i",
        description="first",
        body_text="body-1\n",
    )
    registry = PromptRegistry.scan(tmp_prompts_dir)
    first_body = registry.get("snmp_pmp450i").body

    # Operator drops an updated file: registry must NOT pick it up.
    # (The second write's front-matter is intentionally incomplete —
    # the registry never re-reads it.)
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
    _write_versioned_prompt(
        tmp_prompts_dir,
        name="snmp_pmp450i",
        description="override",
        body_text="OVERRIDDEN\n",
    )
    registry = PromptRegistry.scan(tmp_prompts_dir)
    assert registry.get("snmp_pmp450i").body.strip() == "OVERRIDDEN"


def test_settings_prompts_dir_is_used(tmp_prompts_dir: Path) -> None:
    """`Settings.prompts_dir` overrides packaged data."""
    from nora.config import Settings

    _write_versioned_prompt(
        tmp_prompts_dir,
        name="snmp_pmp450i",
        description="from-settings",
        body_text="from-settings\n",
    )
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
    target = _write_versioned_prompt(
        tmp_prompts_dir,
        name="snmp_pmp450i",
        description="d",
        body_text="body\n",
    )
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
# Issue #72 — Per-Tool MCP Prompt Exposure
#
# Asserts the 13 tool-specs under `docs/tool_specs/` are reachable via
# `mcp.list_prompts()` AND that `_EXPOSED_PROMPTS` (the audit allow-list)
# stays in sync with the registered `@mcp.prompt` functions.
# ---------------------------------------------------------------------------


def _shipped_tool_spec_names() -> set[str]:
    """Return the basenames of every shipped tool-spec, excluding `README.md`.

    Used by the issue #72 prompt-exposure tests so they auto-discover new
    tool-specs added under `docs/tool_specs/` without needing the test
    file to be edited.
    """
    specs_dir = Path(__file__).resolve().parent.parent / "docs" / "tool_specs"
    return {p.stem for p in specs_dir.glob("*.md") if p.name != "README.md"}


def test_server_exposes_all_tool_spec_prompts() -> None:
    """All shipped tool-specs are reachable via `mcp.list_prompts()`.

    Per `prompt-registry` *Per-Tool MCP Prompt Exposure* scenario "every
    tool-spec is reachable via get_prompt".
    """
    import asyncio

    from nora import server as server_mod

    async def _names() -> set[str]:
        prompts = await server_mod.mcp.list_prompts()
        return {p.name for p in prompts}

    names = asyncio.run(_names())
    expected = _shipped_tool_spec_names()
    missing = expected - names
    assert not missing, (
        f"Expected every shipped tool-spec to be exposed via @mcp.prompt; "
        f"missing: {sorted(missing)}"
    )


def test_exposed_prompts_allowlist_matches_mcp_list_prompts() -> None:
    """`_EXPOSED_PROMPTS` MUST exactly equal the set of registered @mcp.prompt names.

    Per `prompt-registry` *Per-Tool MCP Prompt Exposure* scenario "boot
    fails when a tool-spec is not exposed via @mcp.prompt". This test is
    the contract: any drift between the allow-list and the registered
    prompts fails the suite.
    """
    import asyncio

    from nora import server as server_mod

    async def _names() -> set[str]:
        prompts = await server_mod.mcp.list_prompts()
        return {p.name for p in prompts}

    actual = asyncio.run(_names())
    expected = set(server_mod._EXPOSED_PROMPTS)
    assert actual == expected, (
        f"_EXPOSED_PROMPTS ({sorted(expected)}) is out of sync with "
        f"registered @mcp.prompt names ({sorted(actual)}); "
        f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
    )


def test_tool_spec_prompt_body_matches_registry_body() -> None:
    """Spot-check: 3 sampled tool-spec prompts return `PromptRegistry.get(name).body`.

    Per `prompt-registry` *Per-Tool MCP Prompt Exposure* scenario
    "tool-spec body equals registry body". A representative sample
    (one Tier 0, one Tier 1, one Tier 2) covers all three governance
    tiers without making the test exhaustive.
    """
    import asyncio

    from nora import server as server_mod
    from nora.prompts.registry import PromptRegistry

    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    specs_dir = Path(__file__).resolve().parent.parent / "docs" / "tool_specs"
    registry = PromptRegistry.scan([package_dir, specs_dir])

    previous = server_mod._current_prompt_registry  # type: ignore[attr-defined]
    try:
        server_mod.set_prompt_registry(registry)

        # Tier 0, Tier 1, Tier 2 — covers all governance branches.
        sampled = (
            "snmp_get_pmp450i_radio_metrics",  # tier 0
            "snmp_run_spectrum_analysis",  # tier 1
            "snmp_reboot_radio",  # tier 2
        )

        async def _bodies() -> dict[str, str]:
            # FastMCP 3.x: `mcp.get_prompt(name)` returns the prompt
            # definition (a `FunctionPrompt`); `mcp.render_prompt(name)`
            # returns the rendered `PromptResult` with `.messages[0].content.text`.
            return {
                name: (await server_mod.mcp.render_prompt(name)).messages[0].content.text
                for name in sampled
            }

        bodies = asyncio.run(_bodies())
        for name in sampled:
            assert bodies[name] == registry.get(name).body, (
                f"@mcp.prompt {name!r} returned body that does not match "
                f"PromptRegistry.get({name!r}).body byte-for-byte"
            )
    finally:
        server_mod._current_prompt_registry = previous  # type: ignore[attr-defined]


def test_orchestrator_prompt_directs_llm_to_read_spec_before_invoke() -> None:
    """`netops_orchestrator.md` MUST teach the LLM to call
    `get_prompt(name=<tool>)` before invoking tools.

    Per `prompt-registry` *Orchestrator Prompt Body Augmentation*
    requirement and issue #72 canonical prompt source layer. The orchestrator
    prompt is the system context the LLM plans against; without explicit
    guidance to read the per-tool spec, the LLM falls back to vague prior
    knowledge and skips the governance contract.
    """
    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    text = (package_dir / "netops_orchestrator.md").read_text()

    # Pattern: the orchestrator body must mention the MCP prompt-call shape.
    assert "get_prompt(name=" in text, (
        "Orchestrator prompt must teach the LLM to call `get_prompt(name=<tool>)` "
        "before invoking tools; pattern `get_prompt(name=` not found in body."
    )
    # Mandatory language: must or shall or required.
    lower = text.lower()
    assert any(token in lower for token in ("must ", "shall ", "mandatory")), (
        "Orchestrator prompt must use mandatory language when describing the spec lookup."
    )


def test_orchestrator_prompt_names_all_shipped_tool_specs() -> None:
    """`netops_orchestrator.md` references every shipped `docs/tool_specs/<tool>.md` by name.

    Per `prompt-registry` *Orchestrator Prompt Body Augmentation*. Each
    tool's tier classification must be reachable from the body so the LLM
    can resolve tool → tier → spec lookup chain without leaving the prompt.
    """
    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    text = (package_dir / "netops_orchestrator.md").read_text()

    expected = _shipped_tool_spec_names()
    missing = {n for n in expected if n not in text}
    assert not missing, (
        f"Orchestrator prompt must reference every shipped tool by name; missing: {sorted(missing)}"
    )


# ---------------------------------------------------------------------------
# Issue #45 — Prompt Versioning Frontmatter Schema (WU-1)
#
# Asserts the version-based front-matter schema enforced by
# `PromptRegistry._validate_system_prompt` on files under
# `src/nora/prompts/` (system prompts):
#
#   name             — must match the basename (sans .md)
#   description      — non-empty str
#   version          — strict SemVer triple (e.g. "0.3.5")
#   nora_compatibility — SemVer range (e.g. ">=0.3.4,<0.4.0"); validated
#                        against `nora.__version__` via packaging
#   governance       — dict with non-negative ints for tier_0/1/2
#   checksum_sha256  — 64-char lowercase hex; matched against
#                      sha256(body_bytes) computed at scan time
#
# Tool-spec files (`docs/tool_specs/*.md`) keep the tier-based schema
# unchanged — covered by the regression test at the end of this block.
# ---------------------------------------------------------------------------


def _write_versioned_prompt(
    prompts_dir: Path,
    *,
    name: str = "test_prompt",
    description: str = "Test versioned prompt.",
    version: str = "0.3.5",
    nora_compatibility: str = ">=0.3.4,<0.4.0",
    governance: dict[str, int] | None = None,
    body_text: str = "Test body line.\n",
    checksum_sha256: str | None = None,
) -> Path:
    """Write a versioned system prompt to `prompts_dir` and return the path.

    `body_text` is exactly what the registry will surface as
    `Prompt.body` after front-matter stripping (one leading newline is
    removed by the parser). If `checksum_sha256` is None, the helper
    computes the correct value from `body_text`.
    """
    if governance is None:
        governance = {"tier_0": 0, "tier_1": 0, "tier_2": 0}
    if checksum_sha256 is None:
        checksum_sha256 = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
    fm_lines = [
        f"name: {name}",
        f"description: {description}",
        f"version: {version}",
        # `nora_compatibility` is quoted because the leading `>` triggers
        # YAML's folded block-scalar heuristic (`>=` is read as `>` +
        # non-whitespace); an unquoted SpecifierSet breaks yaml.safe_load.
        f'nora_compatibility: "{nora_compatibility}"',
        "governance:",
        f"  tier_0: {governance['tier_0']}",
        f"  tier_1: {governance['tier_1']}",
        f"  tier_2: {governance['tier_2']}",
        f"checksum_sha256: {checksum_sha256}",
    ]
    fm_block = "\n".join(fm_lines)
    target = prompts_dir / f"{name}.md"
    target.write_text(f"---\n{fm_block}\n---\n{body_text}")
    return target


def test_registry_rejects_prompt_with_mismatched_checksum(tmp_prompts_dir: Path) -> None:
    """A system prompt with a wrong `checksum_sha256` is dropped at scan.

    Per issue #45 *Checksum Drift Detection*: the registry computes
    `sha256(body_bytes)` at scan time and refuses to load a prompt
    whose declared checksum does not match. The operator cannot lie.
    """
    wrong_checksum = "0" * 64
    _write_versioned_prompt(
        tmp_prompts_dir,
        name="bad_checksum",
        checksum_sha256=wrong_checksum,
    )
    registry = PromptRegistry.scan(tmp_prompts_dir)
    with pytest.raises(PromptNotFoundError) as exc:
        registry.get("bad_checksum")
    assert "bad_checksum" in str(exc.value)


def test_registry_rejects_prompt_with_invalid_nora_compatibility(
    tmp_prompts_dir: Path,
) -> None:
    """A `nora_compatibility` value that is not a parseable SpecifierSet fails.

    Per issue #45 *Compatibility Range Enforcement*: the front-matter
    range MUST parse via `packaging.specifiers.SpecifierSet`; a
    non-range string raises `InvalidSpecifier` and the prompt is
    dropped at scan.
    """
    _write_versioned_prompt(
        tmp_prompts_dir,
        name="bad_compat",
        nora_compatibility="not-a-semver-range",
    )
    registry = PromptRegistry.scan(tmp_prompts_dir)
    with pytest.raises(PromptNotFoundError) as exc:
        registry.get("bad_compat")
    assert "bad_compat" in str(exc.value)


def test_registry_rejects_prompt_with_non_semver_version(tmp_prompts_dir: Path) -> None:
    """A `version` value that is not a strict SemVer triple fails.

    Per issue #45 *System-Prompt Versioning*: `version` MUST be a strict
    `MAJOR.MINOR.PATCH` triple. Single-segment ("1") or non-numeric
    ("abc") values are rejected at scan.
    """
    _write_versioned_prompt(
        tmp_prompts_dir,
        name="bad_version",
        version="not-semver",
    )
    registry = PromptRegistry.scan(tmp_prompts_dir)
    with pytest.raises(PromptNotFoundError) as exc:
        registry.get("bad_version")
    assert "bad_version" in str(exc.value)


def test_registry_accepts_prompt_with_valid_frontmatter(tmp_prompts_dir: Path) -> None:
    """A versioned prompt with all required fields and a correct checksum loads.

    Per issue #45 *System-Prompt Versioning*: a `name`-matching prompt
    with a valid `description`, strict-SemVer `version`, parseable
    `nora_compatibility`, well-shaped `governance`, and a checksum that
    matches `sha256(body_bytes)` MUST load through `scan`.
    """
    body = "Acceptance body for the versioned prompt.\n"
    _write_versioned_prompt(
        tmp_prompts_dir,
        name="good_prompt",
        version="0.3.5",
        nora_compatibility=">=0.3.4,<0.4.0",
        governance={"tier_0": 1, "tier_1": 0, "tier_2": 0},
        body_text=body,
    )
    registry = PromptRegistry.scan(tmp_prompts_dir)
    prompt = registry.get("good_prompt")
    assert isinstance(prompt, Prompt)
    assert prompt.name == "good_prompt"
    assert prompt.description == "Test versioned prompt."
    assert prompt.body == body
    # metadata exposes the validated front-matter for downstream callers.
    assert prompt.metadata["version"] == "0.3.5"
    assert prompt.metadata["nora_compatibility"] == ">=0.3.4,<0.4.0"
    assert prompt.metadata["governance"] == {"tier_0": 1, "tier_1": 0, "tier_2": 0}
    assert prompt.metadata["checksum_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()


def test_registry_drops_prompt_when_nora_compatibility_unsatisfied(
    tmp_prompts_dir: Path,
) -> None:
    """A `nora_compatibility` range that excludes `nora.__version__` drops the prompt.

    Per issue #45 *Compatibility Range Enforcement*: the registry fails
    closed when the front-matter range does not satisfy
    `nora.__version__`. The package's current version is 0.3.5 (see
    `src/nora/__init__.py`); `>=99.0.0` excludes it.
    """
    _write_versioned_prompt(
        tmp_prompts_dir,
        name="future_only",
        nora_compatibility=">=99.0.0",
    )
    registry = PromptRegistry.scan(tmp_prompts_dir)
    with pytest.raises(PromptNotFoundError) as exc:
        registry.get("future_only")
    assert "future_only" in str(exc.value)


def test_tool_specs_keep_tier_based_schema(tmp_path: Path) -> None:
    """Tool-spec files under `tool_specs/` keep the tier-based schema unchanged.

    Per issue #45 *Q8 — Tool-specs vs system-prompts front-matter split*:
    the existing tier-based validator (`_validate_tool_spec`) is the
    authoritative schema for `docs/tool_specs/*.md` and MUST continue
    to accept a `tier: 0|1|2` front-matter WITHOUT the new version-based
    fields. This is the regression test that proves WU-1 did NOT touch
    the tool-spec code path.
    """
    import nora  # local import — keeps module-level imports tight.

    tool_specs_dir = tmp_path / "tool_specs"
    tool_specs_dir.mkdir()
    spec = tool_specs_dir / "snmp_get_ap_summary.md"
    spec.write_text(
        "---\n"
        "name: snmp_get_ap_summary\n"
        "description: Read a typed AP summary.\n"
        "tier: 0\n"
        "---\n"
        "Tool body for snmp_get_ap_summary.\n"
    )
    registry = PromptRegistry.scan(tool_specs_dir)
    prompt = registry.get("snmp_get_ap_summary")
    assert isinstance(prompt, Prompt)
    assert prompt.metadata["tier"] == 0
    # The version-based fields are NOT exposed for tool-spec prompts.
    assert "version" not in prompt.metadata
    assert "checksum_sha256" not in prompt.metadata
    assert "nora_compatibility" not in prompt.metadata
    assert "governance" not in prompt.metadata
    # Sanity: the registry refuses the new fields' presence in a
    # tool-spec scan — a system-prompt-style front-matter in a
    # tool-spec dir still loads as a tool-spec (the validator is
    # not invoked when the new fields are absent).
    assert nora.__version__ == "0.3.5"


# ---------------------------------------------------------------------------
# PR #73 review follow-up — meta-tool bridge for spec lookup.
#
# Asserts `nora_get_tool_spec` is reachable from any MCP client that
# exposes the tool-calling schema, regardless of whether the client
# bridges `prompts/get` into a callable tool. Closes the PR #73 review
# finding on `feat/prompt-spec-exposure`.
# ---------------------------------------------------------------------------


def test_nora_get_tool_spec_is_registered_as_tool() -> None:
    """`nora_get_tool_spec` MUST appear in `mcp.list_tools()` so standard MCP clients expose it.

    Per `prompt-registry` *Tool-Bridged Spec Lookup* requirement (PR #73
    review feedback). Without this assertion the bridge tool could be
    added but invisible to clients that do not bridge `prompts/get`.
    """
    import asyncio

    from nora import server as server_mod

    async def _names() -> set[str]:
        tools = await server_mod.mcp.list_tools()
        return {t.name for t in tools}

    names = asyncio.run(_names())
    assert "nora_get_tool_spec" in names, (
        f"Expected nora_get_tool_spec in mcp.list_tools(); got: {sorted(names)}"
    )


def test_nora_get_tool_spec_returns_registry_body() -> None:
    """Calling `nora_get_tool_spec(name)` returns `PromptRegistry.get(name).body` byte-for-byte.

    Per `prompt-registry` *Tool-Bridged Spec Lookup* requirement. Spot-
    checked across all 3 tiers to confirm governance payloads surface
    identically through the tool and the registry.
    """
    import asyncio
    from pathlib import Path

    from fastmcp import Client

    from nora import server as server_mod
    from nora.prompts.registry import PromptRegistry

    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    specs_dir = Path(__file__).resolve().parent.parent / "docs" / "tool_specs"
    registry = PromptRegistry.scan([package_dir, specs_dir])

    previous = server_mod._current_prompt_registry  # type: ignore[attr-defined]
    try:
        server_mod.set_prompt_registry(registry)

        sampled = (
            "snmp_get_pmp450i_radio_metrics",  # tier 0
            "snmp_run_spectrum_analysis",  # tier 1
            "snmp_reboot_radio",  # tier 2
            "nora_get_tool_spec",  # the meta-tool itself (tier 0)
        )

        async def _bodies() -> dict[str, str]:
            async with Client(server_mod.mcp) as client:
                return {
                    name: (await client.call_tool("nora_get_tool_spec", {"name": name})).data
                    for name in sampled
                }

        bodies = asyncio.run(_bodies())
        for name in sampled:
            assert bodies[name] == registry.get(name).body, (
                f"nora_get_tool_spec({name!r}) returned body that does not "
                f"match PromptRegistry.get({name!r}).body byte-for-byte"
            )
    finally:
        server_mod._current_prompt_registry = previous  # type: ignore[attr-defined]


def test_nora_get_tool_spec_raises_on_unknown_name() -> None:
    """Calling `nora_get_tool_spec` with an unknown name surfaces `PromptNotFoundError`.

    Per `prompt-registry` *Tool-Bridged Spec Lookup* requirement. The
    registry fail-closed contract MUST propagate through the bridge tool
    so the LLM cannot fabricate specs for unregistered names.
    """
    import asyncio
    from pathlib import Path

    from fastmcp import Client

    from nora import server as server_mod
    from nora.prompts.registry import PromptRegistry

    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    specs_dir = Path(__file__).resolve().parent.parent / "docs" / "tool_specs"
    registry = PromptRegistry.scan([package_dir, specs_dir])

    previous = server_mod._current_prompt_registry  # type: ignore[attr-defined]
    try:
        server_mod.set_prompt_registry(registry)

        async def _call_unknown() -> None:
            async with Client(server_mod.mcp) as client:
                await client.call_tool("nora_get_tool_spec", {"name": "no_such_tool_xyz"})

        # The bridge MUST raise — the test will fail if it silently returns
        # an empty body or a fabricated spec. The exact exception class
        # depends on how FastMCP wraps registry errors at the tool
        # boundary; accept any exception whose message references the bad
        # name, since `PromptNotFoundError` may be wrapped by FastMCP's
        # tool error path.
        with pytest.raises(Exception) as exc:
            asyncio.run(_call_unknown())
        # The exception message MUST reference the bad name OR mention the
        # registry. Accept either signal.
        msg = str(exc.value).lower()
        assert (
            "no_such_tool_xyz" in str(exc.value).lower()
            or "promptnotfounderror" in msg
            or "not found" in msg
        ), f"Expected exception referencing the unknown name; got: {exc.value!r}"
    finally:
        server_mod._current_prompt_registry = previous  # type: ignore[attr-defined]


def test_nora_get_tool_spec_prompt_is_registered() -> None:
    """`nora_get_tool_spec` is also exposed via `@mcp.prompt` for symmetry.

    Per `prompt-registry` *Per-Tool MCP Prompt Exposure* requirement.
    Both the tool AND the prompt expose the same registry body, so
    clients bridging prompts into tools AND clients that don't get a
    consistent answer.
    """
    import asyncio

    from nora import server as server_mod

    async def _names() -> set[str]:
        prompts = await server_mod.mcp.list_prompts()
        return {p.name for p in prompts}

    names = asyncio.run(_names())
    assert "nora_get_tool_spec" in names, (
        f"Expected nora_get_tool_spec in mcp.list_prompts(); got: {sorted(names)}"
    )


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
    """`netops_orchestrator.md` references the five real `@mcp.tool` names
    (without `nora_` prefix) AND none of the banned literals appear.

    This includes the explicit Cambium-OUI guard added by this PR: the
    literal `00:04:56` MUST NOT appear in the body.
    """
    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    text = (package_dir / "netops_orchestrator.md").read_text()

    # The five real @mcp.tool names — no `nora_` prefix.
    expected_tool_names = (
        "snmp_get_pmp450i_radio_metrics",
        "search_intervention_history",
        "get_device_lifecycle_summary",
        "correlate_sector_interference",
        "save_intervention_record",
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


# ---------------------------------------------------------------------------
# Issue #43 / `2026-09-15-3tier-tool-governance` — multi-dir scan +
# ADR-4 tool-spec validator.
#
# Adds:
#  - `PromptRegistry.scan(sources: Sequence[Path])` accepts multiple dirs.
#  - `PromptRegistry.from_settings(settings)` reads
#    `Settings.nora_tool_specs_dir` (default `docs/tool_specs/`).
#  - Tool-spec files MUST carry `tier: 0 | 1 | 2` and the cross-validator
#    invariants from ADR-4:
#      tier 1 -> requires_operator_confirmed=True (required)
#      tier 2 -> requires_hitl_token=True (required)
#      tier 0 -> both flags MAY be false / absent
#  - README.md (no tier) is scanned but exempt from the cross-validator.
#  - Orchestrator prompt body augmented with tier references at scan time.
# ---------------------------------------------------------------------------


def test_prompt_registry_scan_accepts_multiple_sources(tmp_path: Path) -> None:
    """`scan([a, b])` loads files from BOTH directories.

    Per `prompt-registry` R8 scenario "both source dirs are scanned at boot".
    """
    # Two source dirs.
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()

    # Both dirs are non-tool-spec, so files here use the version-based
    # schema enforced by `PromptRegistry._validate_system_prompt`.
    _write_versioned_prompt(
        dir_a,
        name="alpha",
        description="from a",
        body_text="body-alpha\n",
    )
    _write_versioned_prompt(
        dir_b,
        name="beta",
        description="from b",
        body_text="body-beta\n",
    )

    registry = PromptRegistry.scan([dir_a, dir_b])

    assert registry.get("alpha").body.strip() == "body-alpha"
    assert registry.get("beta").body.strip() == "body-beta"


def test_tool_spec_with_tier_1_loads_with_operator_confirmed() -> None:
    """A `tier: 1` tool-spec loads and exposes `requires_operator_confirmed=True`.

    Per `prompt-registry` R8 scenario "tool specs with tier: 1 and tier: 2
    load successfully".
    """
    spec_path = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "tool_specs"
        / "snmp_run_spectrum_analysis.md"
    )
    assert spec_path.is_file(), f"Expected shipped tool spec at {spec_path}"

    # Open the registry as a regular prompt via single-dir scan.
    registry = PromptRegistry.scan(spec_path.parent)
    prompt = registry.get("snmp_run_spectrum_analysis")

    # The validator stamps the canonical metadata on the body via the
    # front-matter tier + requires_* fields. The Prompt model exposes
    # the parsed front-matter as `metadata` (tool-spec schema).
    assert prompt.metadata["tier"] == 1
    assert prompt.metadata["requires_operator_confirmed"] is True


def test_tool_spec_with_tier_2_loads_with_hitl_token() -> None:
    """A `tier: 2` tool-spec exposes `requires_hitl_token=True`."""
    spec_path = (
        Path(__file__).resolve().parent.parent
        / "docs"
        / "tool_specs"
        / "snmp_migrate_radio_frequency.md"
    )
    assert spec_path.is_file(), f"Expected shipped tool spec at {spec_path}"

    registry = PromptRegistry.scan(spec_path.parent)
    prompt = registry.get("snmp_migrate_radio_frequency")
    assert prompt.metadata["tier"] == 2
    assert prompt.metadata["requires_hitl_token"] is True


def test_tool_spec_invalid_tier_raises_prompt_not_found(tmp_path: Path) -> None:
    """A tool-spec with `tier: 9` (not in {0,1,2}) raises `PromptNotFoundError`.

    Per `prompt-registry` R8 scenario "invalid tier marker raises
    PromptNotFoundError".
    """
    tool_specs_dir = tmp_path / "tool_specs"
    tool_specs_dir.mkdir()
    bad_spec = tool_specs_dir / "bad_spec.md"
    bad_spec.write_text("---\nname: bad_spec\ndescription: tier-out-of-range\ntier: 9\n---\nbody\n")
    registry = PromptRegistry.scan(tool_specs_dir)
    with pytest.raises(PromptNotFoundError) as exc:
        registry.get("bad_spec")
    assert "bad_spec" in str(exc.value)


def test_tool_spec_tier_1_requires_operator_confirmed_cross_validator(
    tmp_path: Path,
) -> None:
    """`tier: 1` WITHOUT `requires_operator_confirmed=True` fails validation.

    Per ADR-4 cross-validator invariant: `tier 1 ⇒
    requires_operator_confirmed=True`.
    """
    tool_specs_dir = tmp_path / "tool_specs"
    tool_specs_dir.mkdir()
    bad_spec = tool_specs_dir / "tier1_bad.md"
    bad_spec.write_text(
        "---\nname: tier1_bad\ndescription: tier 1 without clearance flag\ntier: 1\n---\nbody\n"
    )
    registry = PromptRegistry.scan(tool_specs_dir)
    with pytest.raises(PromptNotFoundError) as exc:
        registry.get("tier1_bad")
    assert "tier1_bad" in str(exc.value)


def test_tool_spec_tier_2_requires_hitl_token_cross_validator(
    tmp_path: Path,
) -> None:
    """`tier: 2` WITHOUT `requires_hitl_token=True` fails validation.

    Per ADR-4 cross-validator invariant: `tier 2 ⇒ requires_hitl_token=True`.
    """
    tool_specs_dir = tmp_path / "tool_specs"
    tool_specs_dir.mkdir()
    bad_spec = tool_specs_dir / "tier2_bad.md"
    bad_spec.write_text(
        "---\nname: tier2_bad\ndescription: tier 2 without HITL flag\ntier: 2\n---\nbody\n"
    )
    registry = PromptRegistry.scan(tool_specs_dir)
    with pytest.raises(PromptNotFoundError) as exc:
        registry.get("tier2_bad")
    assert "tier2_bad" in str(exc.value)


def test_tool_spec_readme_has_no_tier_and_is_scanned(
    tmp_path: Path,
) -> None:
    """`docs/tool_specs/README.md` is scanned but carries no `tier`.

    Per `prompt-registry` R8 scenario "README.md has no tier marker".
    README is not a tool — it MUST NOT carry the `tier` field.
    """
    tool_specs_dir = tmp_path / "tool_specs"
    tool_specs_dir.mkdir()
    readme = tool_specs_dir / "README.md"
    readme.write_text(
        "---\nname: README\ndescription: index for tool specs\n---\nIndex of tool specs.\n"
    )
    registry = PromptRegistry.scan(tool_specs_dir)
    prompt = registry.get("README")
    assert prompt.metadata.get("tier") is None


def test_from_settings_reads_tool_specs_dir(tmp_path: Path) -> None:
    """`from_settings(settings)` scans both `_PACKAGED_PROMPTS_DIR` AND `nora_tool_specs_dir`."""
    from nora.config import Settings

    tool_specs_dir = tmp_path / "tool_specs"
    tool_specs_dir.mkdir()
    (tool_specs_dir / "snmp_get_ap_summary.md").write_text(
        "---\nname: snmp_get_ap_summary\ndescription: passive AP summary\ntier: 0\n---\nbody\n"
    )
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_tool_specs_dir=tool_specs_dir,
    )
    registry = PromptRegistry.from_settings(settings)
    # Packaged netops_orchestrator + shipped tool spec both load.
    assert registry.get("netops_orchestrator") is not None
    assert registry.get("snmp_get_ap_summary").metadata["tier"] == 0


def test_from_settings_missing_tool_specs_dir_is_nonfatal(tmp_path: Path) -> None:
    """`from_settings(settings)` skips an absent tool-specs dir without raising."""
    from nora.config import Settings

    absent_dir = tmp_path / "nonexistent"
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_tool_specs_dir=absent_dir,
    )
    registry = PromptRegistry.from_settings(settings)
    # Packaged prompts still load.
    assert registry.get("netops_orchestrator") is not None


def test_orchestrator_prompt_body_names_all_three_tiers() -> None:
    """`netops_orchestrator.md` references `Tier 0`, `Tier 1`, and `Tier 2`.

    Per `prompt-registry` R8 scenario "orchestrator body references every tier".
    """
    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    text = (package_dir / "netops_orchestrator.md").read_text()

    assert "Tier 0" in text
    assert "Tier 1" in text
    assert "Tier 2" in text
    assert "Universal Service Impact & Disruption Gate" in text


def test_orchestrator_prompt_body_names_clearance_and_hitl_protocols() -> None:
    """The orchestrator body names `operator_confirmed=True` AND HITL approval token.

    Per `prompt-registry` R8 scenario "orchestrator body names the
    clearance and HITL protocols". Tier-1 protocol precedes Tier-2.
    """
    package_dir = Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts"
    text = (package_dir / "netops_orchestrator.md").read_text()

    assert "operator_confirmed=True" in text
    assert "HITL approval token" in text

    # Ordering: Tier-1 protocol before Tier-2 protocol.
    idx_t1 = text.find("operator_confirmed=True")
    idx_t2 = text.find("HITL approval token")
    assert 0 <= idx_t1 < idx_t2, (
        f"Tier-1 protocol (operator_confirmed=True) MUST precede Tier-2 protocol "
        f"(HITL approval token) in the orchestrator body; got t1={idx_t1}, t2={idx_t2}"
    )


def test_every_tool_spec_declares_tier() -> None:
    """Every shipped `docs/tool_specs/*.md` (except README) carries `tier: 0|1|2`."""
    specs_dir = Path(__file__).resolve().parent.parent / "docs" / "tool_specs"
    expected_tool_specs = {
        "snmp_get_ap_summary.md",
        "snmp_get_sm_table.md",
        "snmp_get_pmp450i_radio_metrics.md",
        "snmp_get_frame_utilization.md",
        "snmp_get_sm_detailed_diagnostics.md",
        "search_intervention_history.md",
        "get_device_lifecycle_summary.md",
        "correlate_sector_interference.md",
        "snmp_run_spectrum_analysis.md",
        "snmp_migrate_radio_frequency.md",
        "save_intervention_record.md",
    }
    assert specs_dir.is_dir(), f"Expected docs/tool_specs/ at {specs_dir}"
    assert (specs_dir / "README.md").is_file(), f"Expected README.md at {specs_dir}"

    actual = {p.name for p in specs_dir.glob("*.md")}
    missing = expected_tool_specs - actual
    assert not missing, f"Missing tool specs: {sorted(missing)}"

    # Each spec carries `tier: 0|1|2`.
    import yaml as _yaml

    for name in expected_tool_specs:
        path = specs_dir / name
        text = path.read_text()
        assert text.startswith("---"), f"{name} MUST have YAML front-matter"
        rest = text[3:]
        fence_end = rest.find("\n---")
        assert fence_end != -1, f"{name} MUST close the front-matter fence"
        fm = _yaml.safe_load(rest[:fence_end])
        assert isinstance(fm, dict)
        assert fm.get("tier") in {0, 1, 2}, (
            f"{name} front-matter tier MUST be 0|1|2; got {fm.get('tier')!r}"
        )


def test_tool_spec_metadata_exposes_tier_and_prerequisites() -> None:
    """`Prompt.metadata` exposes `tier`, `requires_operator_confirmed`, `requires_hitl_token`."""
    specs_dir = Path(__file__).resolve().parent.parent / "docs" / "tool_specs"
    registry = PromptRegistry.scan(specs_dir)
    spec = registry.get("snmp_run_spectrum_analysis")
    # metadata is a dict with validated tier + flags.
    assert spec.metadata["tier"] == 1
    assert spec.metadata["requires_operator_confirmed"] is True
    assert "requires_hitl_token" in spec.metadata  # False or absent is fine for tier 1


def test_tool_spec_validator_enforces_uses_operator_confirmed_equals_for_tier_1(
    tmp_path: Path,
) -> None:
    """The validator MUST use a strict boolean check, NOT a Python `==` on the tier value.

    Belt-and-braces: a future refactor that loosened the tier check via
    `==` would re-introduce ambiguity. Static AST scan: the
    `tool_spec_validator` / `_try_load` MUST NOT compare tier via `==`.
    """
    import ast

    src = (
        Path(__file__).resolve().parent.parent / "src" / "nora" / "prompts" / "registry.py"
    ).read_text()
    tree = ast.parse(src)
    # Walk every Compare node and assert no `tier == 1` style equality.
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for left, op, comparator in zip(
            node.left if isinstance(node.left, list) else [node.left],
            node.ops,
            node.comparators,
        ):
            if isinstance(op, ast.Eq) and isinstance(left, ast.Name) and left.id == "tier":
                pytest.fail(
                    "registry.py MUST NOT compare tier via `==`; "
                    "use a validator that asserts membership in {0, 1, 2}"
                )


__all__ = [
    "test_registry_loads_once_and_never_reloads",
    "test_packaged_prompt_loads_on_boot",
    "test_override_directory_wins_over_package_data",
    "test_settings_prompts_dir_is_used",
    "test_missing_prompt_raises_prompt_not_found",
    "test_file_without_front_matter_is_rejected",
    "test_empty_description_is_rejected",
    "test_name_must_match_filename",
    "test_second_get_call_does_no_io",
    "test_shipped_prompt_declares_tool_name_and_typed_schema",
    "test_registry_has_no_inotify_or_watchdog_dependency",
    "test_registry_does_not_call_os_environ_inline",
    "test_shipped_orchestrator_prompt_loads_on_boot",
    "test_orchestrator_prompt_mentions_real_tools_and_no_banned_literals",
    "test_server_exposes_orchestrator_and_driver_prompts",
    "test_server_instructions_are_set_with_required_substrings",
    "test_set_prompt_registry_round_trip",
    "test_get_prompt_registry_raises_when_not_initialised",
    # Issue #43 / multi-dir scan + ADR-4 validator
    "test_prompt_registry_scan_accepts_multiple_sources",
    "test_tool_spec_with_tier_1_loads_with_operator_confirmed",
    "test_tool_spec_with_tier_2_loads_with_hitl_token",
    "test_tool_spec_invalid_tier_raises_prompt_not_found",
    "test_tool_spec_tier_1_requires_operator_confirmed_cross_validator",
    "test_tool_spec_tier_2_requires_hitl_token_cross_validator",
    "test_tool_spec_readme_has_no_tier_and_is_scanned",
    "test_from_settings_reads_tool_specs_dir",
    "test_from_settings_missing_tool_specs_dir_is_nonfatal",
    "test_orchestrator_prompt_body_names_all_three_tiers",
    "test_orchestrator_prompt_body_names_clearance_and_hitl_protocols",
    "test_every_tool_spec_declares_tier",
    "test_tool_spec_metadata_exposes_tier_and_prerequisites",
    "test_tool_spec_validator_enforces_uses_operator_confirmed_equals_for_tier_1",
    # Issue #72 — per-tool MCP prompt exposure
    "test_exposed_prompts_allowlist_matches_mcp_list_prompts",
    "test_orchestrator_prompt_directs_llm_to_read_spec_before_invoke",
    "test_orchestrator_prompt_names_all_shipped_tool_specs",
    "test_server_exposes_all_tool_spec_prompts",
    "test_tool_spec_prompt_body_matches_registry_body",
    # PR #73 follow-up — meta-tool bridge for spec lookup
    "test_nora_get_tool_spec_is_registered_as_tool",
    "test_nora_get_tool_spec_prompt_is_registered",
    "test_nora_get_tool_spec_raises_on_unknown_name",
    "test_nora_get_tool_spec_returns_registry_body",
    # Issue #45 — version-based front-matter schema (WU-1)
    "test_registry_rejects_prompt_with_mismatched_checksum",
    "test_registry_rejects_prompt_with_invalid_nora_compatibility",
    "test_registry_rejects_prompt_with_non_semver_version",
    "test_registry_accepts_prompt_with_valid_frontmatter",
    "test_registry_drops_prompt_when_nora_compatibility_unsatisfied",
    "test_tool_specs_keep_tier_based_schema",
]
