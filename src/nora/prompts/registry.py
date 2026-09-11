"""Prompt registry — one-shot Markdown loader with front-matter validation.

Contract (Prompt-R1..R7):

* `PromptRegistry.scan(source_dir)` loads every ``*.md`` file at boot.
  The result is frozen for the lifetime of the process; later writes
  to the directory MUST NOT be picked up.
* Front-matter is YAML, parsed with ``yaml.safe_load``. The ``name``
  field MUST match the file basename (sans ``.md``); ``description``
  MUST be a non-empty string. Both contract failures raise
  `PromptNotFoundError`.
* `get(name)` is in-memory after `scan`; the second call MUST NOT
  trigger any I/O.
* The default source is the packaged prompts directory
  ``src/nora/prompts/``. Operators can override via
  ``Settings.prompts_dir``; the registry never reads the process
  environment directly.
* `PromptNotFoundError` is raised for every failure mode — missing
  file, missing front-matter, empty description, mismatched name, etc.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict

from nora.config import Settings
from nora.drivers.exceptions import PromptNotFoundError

# `src/nora/prompts/` — the packaged source of truth, resolved relative
# to this file so tests that monkey-patch `Path` still find it.
_PACKAGED_PROMPTS_DIR: Path = Path(__file__).resolve().parent


class Prompt(BaseModel):
    """A single loaded prompt.

    `body` is the Markdown content AFTER the YAML front-matter block
    (the block itself is consumed by the loader and not stored).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    body: str


class PromptRegistry:
    """Immutable registry of system prompts, scanned once at boot.

    Hot-reload is forbidden by spec; every operation that would mutate
    the registry's contents is intentionally absent.
    """

    def __init__(self, *, _prompts: dict[str, Prompt], _source_dir: Path) -> None:
        # Internal use only — the public API is `scan()` + `get()`.
        self._prompts = _prompts
        self._source_dir = _source_dir

    @property
    def source_dir(self) -> Path:
        return self._source_dir

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
    def scan(cls, source: Path) -> "PromptRegistry":
        """Scan `source` for `*.md` files and load them all.

        Files that fail front-matter validation are dropped silently
        in this pass; the FIRST failure surfaces as `PromptNotFoundError`
        only when `get(name)` is later called for the missing prompt.
        Missing front-matter / wrong name / empty description all keep
        the file out of the registry.
        """
        prompts: dict[str, Prompt] = {}
        if not source.exists():
            return cls(_prompts=prompts, _source_dir=source)

        for path in sorted(source.glob("*.md")):
            prompt = _try_load(path)
            if prompt is not None:
                prompts[prompt.name] = prompt

        return cls(_prompts=prompts, _source_dir=source)

    @classmethod
    def from_settings(cls, settings: Settings) -> "PromptRegistry":
        """Resolve the prompt source from `Settings.prompts_dir`.

        `Settings.prompts_dir` is the operator override; when `None`
        the registry falls back to the packaged prompts directory.
        """
        override = settings.nora_prompts_dir
        source: Path = override if override is not None else _PACKAGED_PROMPTS_DIR
        return cls.scan(source)


# ---------------------------------------------------------------------------
# Internals — front-matter parsing
# ---------------------------------------------------------------------------


def _try_load(path: Path) -> Prompt | None:
    """Parse `path` and return a `Prompt`, or `None` if the file is invalid.

    Validity rules (Prompt-R5):

    * The file MUST begin with a YAML front-matter block delimited by
      a ``---`` line at the top and a second ``---`` line.
    * The parsed front-matter MUST be a dict with `name` (matching the
      file basename) and a non-empty `description`.
    * Any parse / validation failure yields `None` (silent drop).
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

    return Prompt(name=name, description=description, body=body_text)


__all__ = ["Prompt", "PromptRegistry"]
