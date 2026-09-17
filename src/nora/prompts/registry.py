"""Prompt registry — one-shot Markdown loader with front-matter validation.

Contract (Prompt-R1..R8):

* `PromptRegistry.scan(source)` (single dir, backwards-compatible) AND
  `PromptRegistry.scan([a, b, ...])` (multi-dir, additive per R8) load
  every ``*.md`` file at boot. The result is frozen for the lifetime
  of the process; later writes to any source dir MUST NOT be picked up.
* Front-matter is YAML, parsed with ``yaml.safe_load``. Every scanned
  file MUST have ``name`` matching the file basename (sans ``.md``)
  AND a non-empty ``description``. Tool-spec files (under
  ``Settings.nora_tool_specs_dir``) additionally carry ``tier: 0|1|2``
  with the ADR-4 cross-validator invariants:
      - tier 1 ⇒ ``requires_operator_confirmed=True`` (required)
      - tier 2 ⇒ ``requires_hitl_token=True`` (required)
      - tier 0 ⇒ both flags MAY be false or absent
  Files outside a tool-spec dir MAY omit the tier field. README.md is
  always exempt from tier validation (it is not a tool).
* `get(name)` is in-memory after `scan`; the second call MUST NOT
  trigger any I/O.
* The default source is the packaged prompts directory
  ``src/nora/prompts/``. Operators can override via
  ``Settings.nora_prompts_dir``; tool specs are loaded from
  ``Settings.nora_tool_specs_dir`` (default ``docs/tool_specs/``).
  The registry never reads the process environment directly.
* `PromptNotFoundError` is raised for every failure mode — missing
  file, missing front-matter, empty description, mismatched name, bad
  tier, or any cross-validator miss.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from nora.config import Settings
from nora.drivers.exceptions import PromptNotFoundError

# `src/nora/prompts/` — the packaged source of truth, resolved relative
# to this file so tests that monkey-patch `Path` still find it.
_PACKAGED_PROMPTS_DIR: Path = Path(__file__).resolve().parent

# ADR-4 frozen tier set — the cross-validator asserts membership here.
_VALID_TIERS: frozenset[int] = frozenset({0, 1, 2})


class Prompt(BaseModel):
    """A single loaded prompt (or tool spec).

    `body` is the Markdown content AFTER the YAML front-matter block
    (the block itself is consumed by the loader and not stored).
    `metadata` carries the validated front-matter as a dict so
    downstream callers can inspect ``tier`` / ``requires_operator_confirmed``
    / ``requires_hitl_token`` without re-parsing the file. Frozen so
    the in-memory copy cannot drift from the on-disk file.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    body: str
    metadata: dict[str, Any] = {}  # validated front-matter (tier, flags, ...)


class PromptRegistry:
    """Immutable registry of system prompts, scanned once at boot.

    Hot-reload is forbidden by spec; every operation that would mutate
    the registry's contents is intentionally absent.
    """

    def __init__(
        self,
        *,
        _prompts: dict[str, Prompt],
        _source_dirs: Sequence[Path],
    ) -> None:
        # Internal use only — the public API is `scan()` + `get()`.
        self._prompts = _prompts
        self._source_dirs = tuple(_source_dirs)

    @property
    def source_dirs(self) -> tuple[Path, ...]:
        return self._source_dirs

    @property
    def names(self) -> list[str]:
        """Return every prompt name loaded."""
        return sorted(self._prompts.keys())

    def get(self, name: str) -> Prompt:
        """Return the loaded prompt for `name`.

        Raises `PromptNotFoundError` if no prompt with that name was
        scanned at boot. The second call with the same name performs
        no I/O — the result is served from the in-memory dict.
        """
        try:
            return self._prompts[name]
        except KeyError as exc:
            raise PromptNotFoundError(name) from exc

    # ------------------------------------------------------------------
    # Boot-time construction
    # ------------------------------------------------------------------

    @classmethod
    def scan(cls, source: Path | Sequence[Path]) -> "PromptRegistry":
        """Scan one or more directories for `*.md` files and load them all.

        Single-dir and multi-dir call styles are both supported
        (additive — single-dir calls stay valid per R8). Files that
        fail front-matter validation are dropped silently in this
        pass; the FIRST failure surfaces as `PromptNotFoundError` only
        when `get(name)` is later called for the missing prompt.
        Missing front-matter / wrong name / empty description / bad
        tier all keep the file out of the registry.
        """
        sources: list[Path] = _normalize_sources(source)
        prompts: dict[str, Prompt] = {}
        for src in sources:
            if not src.exists():
                continue
            for path in sorted(src.glob("*.md")):
                prompt = _try_load(path, src)
                if prompt is not None:
                    prompts[prompt.name] = prompt
        return cls(_prompts=prompts, _source_dirs=sources)

    @classmethod
    def from_settings(cls, settings: Settings) -> "PromptRegistry":
        """Resolve the prompt sources from `Settings`.

        `Settings.nora_prompts_dir` is the packaged-prompt operator
        override (falls back to ``_PACKAGED_PROMPTS_DIR`` when None).
        `Settings.nora_tool_specs_dir` is the tool-spec dir
        (default ``Path('docs/tool_specs')``, skipped when absent or
        None). Both dirs are scanned together so the registry is the
        single source of truth for "what is registered at boot".
        """
        prompt_override = settings.nora_prompts_dir
        sources: list[Path] = [
            prompt_override if prompt_override is not None else _PACKAGED_PROMPTS_DIR
        ]
        tool_specs_dir = getattr(settings, "nora_tool_specs_dir", None)
        if tool_specs_dir is not None and tool_specs_dir.exists():
            sources.append(tool_specs_dir)
        return cls.scan(sources)


# ---------------------------------------------------------------------------
# Internals — front-matter parsing + ADR-4 tool-spec validator.
# ---------------------------------------------------------------------------


def _normalize_sources(source: Path | Sequence[Path]) -> list[Path]:
    """Coerce ``source`` to a list of ``Path`` (single or multi-dir)."""
    if isinstance(source, Path):
        return [source]
    if isinstance(source, Iterable) and not isinstance(source, (str, bytes)):
        return [Path(p) for p in source]
    raise TypeError(f"PromptRegistry.scan expects Path or Sequence[Path]; got {type(source)!r}")


def _looks_like_tool_spec_dir(src: Path) -> bool:
    """True when ``src`` looks like the tool-spec dir.

    Heuristic: the tool-spec dir is the one resolved against the
    ``Settings.nora_tool_specs_dir`` path. We rely on the path stem
    — ``docs/tool_specs`` — for the heuristic so the same call site
    works for unit tests (any dir under a ``tool_specs`` name).
    """
    return src.name == "tool_specs"


def _is_readme_file(path: Path) -> bool:
    """README.md is always exempt from ADR-4 tier validation."""
    return path.stem == "README"


def _try_load(path: Path, source_dir: Path) -> Prompt | None:
    """Parse ``path`` and return a ``Prompt``, or ``None`` if invalid.

    Validity rules (Prompt-R5 / R8):

    * The file MUST begin with a YAML front-matter block delimited by
      a ``---`` line at the top and a second ``---`` line.
    * The parsed front-matter MUST be a dict with ``name`` (matching
      the file basename) and a non-empty ``description``.
    * When the source dir is a tool-spec dir (heuristic) AND the
      file is not a README, the ADR-4 cross-validator applies:
        - ``tier`` MUST be present and in ``{0, 1, 2}``.
        - ``tier == 1`` ⇒ ``requires_operator_confirmed == True``.
        - ``tier == 2`` ⇒ ``requires_hitl_token == True``.
    * Any parse / validation failure yields ``None`` (silent drop).
    """
    text = path.read_text()
    if not text.startswith("---"):
        return None
    # Slice off the opening `---` then split on the next `---` line.
    rest = text[3:]
    fence_end = rest.find("\n---")
    if fence_end == -1:
        return None
    fm_text = rest[:fence_end]
    body_text = rest[fence_end + len("\n---") :]
    # Strip a single leading newline so the body starts cleanly.
    if body_text.startswith("\n"):
        body_text = body_text[1:]

    try:
        front_matter: Any = yaml.safe_load(fm_text)
    except yaml.YAMLError:
        return None
    if not isinstance(front_matter, dict):
        return None

    name_raw = front_matter.get("name")
    description_raw = front_matter.get("description")
    if not isinstance(name_raw, str) or not isinstance(description_raw, str):
        return None
    name = name_raw.strip()
    description = description_raw.strip()
    if not description:
        return None
    # The basename (sans `.md`) MUST equal `name`.
    if path.stem != name:
        return None

    # ADR-4 tool-spec validator (applies to tool-spec dir files except README).
    metadata: dict[str, Any] = {}
    if _looks_like_tool_spec_dir(source_dir) and not _is_readme_file(path):
        if not _validate_tool_spec(front_matter, name):
            return None
        metadata = _validated_tool_spec_metadata(front_matter)
    else:
        # Outside a tool-spec dir, capture the raw front-matter minus
        # the fields we already consumed, so callers can inspect the
        # provenance (e.g. ``tier`` when present).
        metadata = {k: v for k, v in front_matter.items() if k not in {"name", "description"}}

    return Prompt(name=name, description=description, body=body_text, metadata=metadata)


def _validate_tool_spec(front_matter: dict[str, Any], name: str) -> bool:
    """ADR-4 cross-validator for tool-spec front-matter.

    Returns ``True`` when the spec is well-formed; ``False`` when any
    invariant is violated. ``PromptNotFoundError`` callers see the
    same fail-closed surface as before — the registry drops invalid
    specs silently; ``get(name)`` raises ``PromptNotFoundError`` when
    the caller later asks for it.
    """
    tier_raw = front_matter.get("tier")
    if not isinstance(tier_raw, int) or tier_raw not in _VALID_TIERS:
        return False

    # Cross-validator invariants — strict membership, NOT `==` on tier.
    requires_operator_confirmed = front_matter.get("requires_operator_confirmed", False)
    requires_hitl_token = front_matter.get("requires_hitl_token", False)

    if 1 in _VALID_TIERS and tier_raw == 1 and requires_operator_confirmed is not True:
        return False
    if 2 in _VALID_TIERS and tier_raw == 2 and requires_hitl_token is not True:
        return False
    return True


def _validated_tool_spec_metadata(front_matter: dict[str, Any]) -> dict[str, Any]:
    """Extract the canonical metadata dict from a tool-spec front-matter.

    Always returns ``tier`` as an ``int`` and the ``requires_*`` flags
    as ``bool`` so downstream callers (server boot wiring, content
    scans) can rely on the type.
    """
    requires_operator_confirmed = bool(front_matter.get("requires_operator_confirmed", False))
    requires_hitl_token = bool(front_matter.get("requires_hitl_token", False))
    return {
        "tier": int(front_matter["tier"]),
        "requires_operator_confirmed": requires_operator_confirmed,
        "requires_hitl_token": requires_hitl_token,
    }


__all__ = ["Prompt", "PromptRegistry"]
