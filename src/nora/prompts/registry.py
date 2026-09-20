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

import hashlib
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
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

    def render(self, name: str) -> str:
        """Return the rendered body for `name`, with the watermark prepended.

        Per issue #45 *Watermark Banner Rendering* (WU-2): the
        rendered body for system prompts (files under
        ``src/nora/prompts/``) carries the Q6-frozen banner

            <!-- NORA-PROMPT: <name> v<version> [sha: <first-8-hex>] -->\n\n<body>

        — a human-readable provenance tag so Open WebUI operators (and
        any future consumer) see which version of the canonical prompt
        the LLM is running against. The ``<!-- ... -->`` comment syntax
        survives markdown rendering in any client.

        Tool-spec files (``docs/tool_specs/*.md``) DO NOT get the banner.
        Per issue #45 *Q8 — Tool-specs vs system-prompts front-matter
        split*, tool-specs keep the tier-based schema and lack the
        ``version`` / ``checksum_sha256`` metadata fields. When those
        keys are absent from ``prompt.metadata``, ``render(name)``
        returns ``prompt.body`` unchanged. The no-op is the design —
        it lets every ``@mcp.prompt`` wrapper switch from
        ``registry.get(name).body`` to ``registry.render(name)`` without
        a per-wrapper conditional.

        Caveats:

        * The ``metadata``-key check is a presence test (``version`` AND
          ``checksum_sha256``), not a value check. If a future contributor
          adds ``version`` or ``checksum_sha256`` to a tool-spec
          front-matter, the watermark WILL render — guard against this
          by keeping the two schemas strictly separate.
        * The SHA is truncated to **8 hex chars** (16 bits, ~1 in 65k
          collision risk) deliberately for human readability. The
          full 64-char digest lives in ``prompt.metadata["checksum_sha256"]``
          for drift detection; the watermark is a non-secret
          provenance tag, NOT a security token. Per the PR-zero-leak
          playbook, HMAC fragments MUST NOT appear in user-visible
          text — the SHA-256 of the body itself is a public checksum,
          and its first 8 hex chars are the readability/footprint
          compromise.
        * ``render(name)`` is **idempotent only for tool-specs**.
          Calling it twice on a system prompt yields two identical
          strings because the banner is computed from immutable
          metadata, but the design intent is one-shot rendering per
          prompt-load, not repeated application.

        Raises:
            PromptNotFoundError: when ``name`` was not scanned at
                boot — same fail-closed contract as ``get(name)``.
        """
        prompt = self.get(name)  # raises PromptNotFoundError for unknown names
        version = prompt.metadata.get("version")
        checksum_sha256 = prompt.metadata.get("checksum_sha256")
        if isinstance(version, str) and isinstance(checksum_sha256, str):
            # System-prompt path — both metadata keys present.
            first_8 = checksum_sha256[:8]
            return f"<!-- NORA-PROMPT: {name} v{version} [sha: {first_8}] -->\n\n{prompt.body}"
        # Tool-spec path (or any future front-matter schema without
        # the version-based fields) — return the body unchanged.
        return prompt.body

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

        Tool-spec dirs are recognised by the basename heuristic
        (``_looks_like_tool_spec_dir``); for callers that need to
        point at a tool-specs dir with a non-standard name, use
        ``from_settings`` (which passes the explicit dir to the
        scanner) or call ``_scan_impl`` directly.
        """
        sources: list[Path] = _normalize_sources(source)
        return cls._scan_impl(sources, tool_specs_dirs=None)

    @classmethod
    def from_settings(cls, settings: Settings) -> "PromptRegistry":
        """Resolve the prompt sources from `Settings`.

        `Settings.nora_prompts_dir` is the packaged-prompt operator
        override (falls back to ``_PACKAGED_PROMPTS_DIR`` when None).
        `Settings.nora_tool_specs_dir` is the tool-spec dir
        (default ``Path('docs/tool_specs')``, skipped when absent or
        None). Both dirs are scanned together so the registry is the
        single source of truth for "what is registered at boot".

        Unlike ``scan``, this passes the explicit tool-specs dir to
        the scanner so operator-overridden paths with non-standard
        names (``my_tool_specs``, etc.) are recognised without
        relying on the basename heuristic.
        """
        prompt_override = settings.nora_prompts_dir
        sources: list[Path] = [
            prompt_override if prompt_override is not None else _PACKAGED_PROMPTS_DIR
        ]
        tool_specs_dir = getattr(settings, "nora_tool_specs_dir", None)
        if tool_specs_dir is not None and tool_specs_dir.exists():
            sources.append(tool_specs_dir)
        return cls._scan_impl(
            sources,
            tool_specs_dirs=[tool_specs_dir] if tool_specs_dir is not None else None,
        )

    @classmethod
    def _scan_impl(
        cls,
        sources: list[Path],
        *,
        tool_specs_dirs: Sequence[Path] | None,
    ) -> "PromptRegistry":
        """Internal scan loop with optional explicit tool-specs identity.

        When ``tool_specs_dirs`` is provided, any source dir whose
        path matches one of those entries is treated as a tool-spec
        dir (tier-based schema); all other source dirs are treated
        as system-prompt dirs (version-based schema — issue #45).
        When ``tool_specs_dirs`` is ``None``, the basename heuristic
        (``_looks_like_tool_spec_dir``) is the only recognition
        mechanism.

        Internal use only — callers should prefer ``scan`` (public
        single/multi-dir API) or ``from_settings`` (Settings-driven).
        """
        prompts: dict[str, Prompt] = {}
        for src in sources:
            if not src.exists():
                continue
            for path in sorted(src.glob("*.md")):
                prompt = _try_load(path, src, tool_specs_dirs=tool_specs_dirs)
                if prompt is not None:
                    prompts[prompt.name] = prompt
        return cls(_prompts=prompts, _source_dirs=tuple(sources))


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
    Falls back to ``False`` when an explicit ``tool_specs_dirs``
    list is supplied — callers that need explicit recognition
    (``from_settings``) pass the list directly.
    """
    return src.name == "tool_specs"


def _is_tool_spec_path(
    src: Path,
    tool_specs_dirs: Sequence[Path] | None,
) -> bool:
    """Decide whether ``src`` should be validated as a tool-spec dir.

    When ``tool_specs_dirs`` is provided, an exact match wins
    regardless of the basename heuristic — this lets
    ``from_settings`` recognise operator-overridden dirs whose
    names do not match the ``tool_specs`` stem. When the list is
    ``None``, the basename heuristic is the sole authority.
    """
    if tool_specs_dirs is not None:
        return any(src == d for d in tool_specs_dirs)
    return _looks_like_tool_spec_dir(src)


def _is_readme_file(path: Path) -> bool:
    """README.md is always exempt from ADR-4 tier validation."""
    return path.stem == "README"


def _try_load(
    path: Path,
    source_dir: Path,
    *,
    tool_specs_dirs: Sequence[Path] | None = None,
) -> Prompt | None:
    """Parse ``path`` and return a ``Prompt``, or ``None`` if invalid.

    Validity rules (Prompt-R5 / R8):

    * The file MUST begin with a YAML front-matter block delimited by
      a ``---`` line at the top and a second ``---`` line.
    * The parsed front-matter MUST be a dict with ``name`` (matching
      the file basename) and a non-empty ``description``.
    * When ``source_dir`` is recognised as a tool-spec dir (explicit
      ``tool_specs_dirs`` match OR basename heuristic) AND the file
      is not a README, the ADR-4 cross-validator applies:
        - ``tier`` MUST be present and in ``{0, 1, 2}``.
        - ``tier == 1`` ⇒ ``requires_operator_confirmed == True``.
        - ``tier == 2`` ⇒ ``requires_hitl_token == True``.
    * Files outside any tool-spec dir are system prompts and MUST
      satisfy the issue #45 version-based schema enforced by
      ``_validate_system_prompt`` (strict SemVer ``version``,
      parseable ``nora_compatibility`` range, ``governance`` dict
      with non-negative ints, matching ``checksum_sha256``).
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
    if _is_tool_spec_path(source_dir, tool_specs_dirs):
        if _is_readme_file(path):
            # README in tool-spec dir — exempt from ADR-4 tier validation.
            metadata = {k: v for k, v in front_matter.items() if k not in {"name", "description"}}
        else:
            if not _validate_tool_spec(front_matter, name):
                return None
            metadata = _validated_tool_spec_metadata(front_matter)
    else:
        # Outside a tool-spec dir = system prompt (issue #45 schema).
        # `_validate_system_prompt` raises `PromptNotFoundError` on every
        # schema / checksum / compatibility miss; we drop silently so the
        # existing scan-time silent-drop contract holds and `get(name)`
        # surfaces the failure as `PromptNotFoundError`.
        try:
            _validate_system_prompt(name, front_matter, body_bytes=body_text.encode("utf-8"))
        except PromptNotFoundError:
            return None
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


def _validate_system_prompt(
    name: str,
    front_matter: dict[str, Any],
    *,
    body_bytes: bytes,
) -> None:
    """Issue #45 — version-based front-matter validator for system prompts.

    Enforced schema for files under ``src/nora/prompts/`` (NOT for
    ``docs/tool_specs/*.md``, which keep the tier-based schema
    enforced by ``_validate_tool_spec``):

        name               — str; MUST equal the `name` argument
        description        — non-empty str
        version            — strict SemVer triple (e.g. ``"0.3.5"``)
        nora_compatibility — SemVer range (e.g. ``">=0.3.4,<0.4.0"``)
                             parseable via ``SpecifierSet``; the
                             range MUST contain ``nora.__version__``
        governance         — dict with non-negative ``int`` values for
                             ``tier_0``, ``tier_1``, ``tier_2``
        checksum_sha256    — 64-char lowercase hex; MUST equal
                             ``hashlib.sha256(body_bytes).hexdigest()``

    Any violation raises ``PromptNotFoundError`` so the existing
    silent-drop scan contract (``_try_load`` catches and returns
    ``None``) holds; the failure surfaces as ``PromptNotFoundError``
    only when ``get(name)`` is later called for the missing prompt.
    """
    # `name` MUST be present and match the basename already verified by
    # ``_try_load``; re-checking guards against schema drift.
    name_raw = front_matter.get("name")
    if not isinstance(name_raw, str) or name_raw.strip() != name:
        raise PromptNotFoundError(
            f"system prompt {name!r}: front-matter `name` field "
            f"is missing or does not match the file basename"
        )

    description_raw = front_matter.get("description")
    if not isinstance(description_raw, str) or not description_raw.strip():
        raise PromptNotFoundError(
            f"system prompt {name!r}: front-matter `description` is missing or empty"
        )

    version_raw = front_matter.get("version")
    if not isinstance(version_raw, str):
        raise PromptNotFoundError(
            f"system prompt {name!r}: front-matter `version` is missing or not a string"
        )
    try:
        Version(version_raw)
    except InvalidVersion as exc:
        raise PromptNotFoundError(
            f"system prompt {name!r}: front-matter `version` "
            f"{version_raw!r} is not a strict SemVer triple "
            f"({exc})"
        ) from exc

    compat_raw = front_matter.get("nora_compatibility")
    if not isinstance(compat_raw, str):
        raise PromptNotFoundError(
            f"system prompt {name!r}: front-matter `nora_compatibility` is missing or not a string"
        )
    try:
        specifier = SpecifierSet(compat_raw)
    except InvalidSpecifier as exc:
        raise PromptNotFoundError(
            f"system prompt {name!r}: front-matter `nora_compatibility` "
            f"{compat_raw!r} is not a parseable SemVer range ({exc})"
        ) from exc

    governance_raw = front_matter.get("governance")
    if not isinstance(governance_raw, dict):
        raise PromptNotFoundError(
            f"system prompt {name!r}: front-matter `governance` is missing or not a dict"
        )
    for tier_key in ("tier_0", "tier_1", "tier_2"):
        tier_value = governance_raw.get(tier_key)
        # `bool` is a subclass of `int` in Python — reject explicitly so
        # `governance: {tier_0: True}` cannot pass the schema.
        if isinstance(tier_value, bool) or not isinstance(tier_value, int):
            raise PromptNotFoundError(
                f"system prompt {name!r}: front-matter `governance.{tier_key}` "
                f"is missing or not an int (got {tier_value!r})"
            )
        if tier_value < 0:
            raise PromptNotFoundError(
                f"system prompt {name!r}: front-matter `governance.{tier_key}` "
                f"is negative ({tier_value}); tier counts MUST be non-negative"
            )

    checksum_raw = front_matter.get("checksum_sha256")
    if not isinstance(checksum_raw, str):
        raise PromptNotFoundError(
            f"system prompt {name!r}: front-matter `checksum_sha256` is missing or not a string"
        )
    expected = hashlib.sha256(body_bytes).hexdigest()
    if not _is_64_char_lowercase_hex(checksum_raw):
        raise PromptNotFoundError(
            f"system prompt {name!r}: front-matter `checksum_sha256` "
            f"{checksum_raw!r} is not a 64-char lowercase hex string"
        )
    if checksum_raw != expected:
        raise PromptNotFoundError(
            f"system prompt {name!r}: checksum mismatch — "
            f"declared {checksum_raw!r} does not match computed "
            f"{expected!r}"
        )

    # Compatibility check — imported lazily to keep the module import
    # surface tight and to avoid any circular-import risk if `nora.__init__`
    # ever pulls in registry machinery at boot.
    from nora import __version__ as nora_version

    if nora_version not in specifier:
        raise PromptNotFoundError(
            f"system prompt {name!r}: `nora_compatibility` range "
            f"{compat_raw!r} does not include nora.__version__ "
            f"{nora_version!r}"
        )


_HEX_CHARS: frozenset[str] = frozenset("0123456789abcdef")


def _is_64_char_lowercase_hex(value: str) -> bool:
    """True when ``value`` is exactly 64 chars from ``[0-9a-f]``."""
    return len(value) == 64 and all(c in _HEX_CHARS for c in value)


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
