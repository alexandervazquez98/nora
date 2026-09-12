"""Storage layer — read-only glob + tolerant per-file parser.

Mirrors `src/nora/core/session_journal.py:_load_or_create_or_recover`
(lines 452-469): per-file `try/except (json.JSONDecodeError,
ValidationError)` → `logger.warning(filename); continue`. One bad file
cannot take down the tool.

Hard read-only rule (R2): this module MUST NOT create the
interventions directory. NORA does not own the dir; openchat's writer
does. If the dir is absent, return `[]` and let the caller decide
whether to surface `NO_HISTORY_FOUND` or empty search results.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import ValidationError

from nora.intervention_memory.models import InterventionMemoryRecord

logger = logging.getLogger(__name__)

# WARNING message format kept as a constant so future log scrapers have
# a stable contract. Format: "{filename}: {error_class}: {error_summary}".
_WARNING_FORMAT: str = "intervention_memory: skipping %s: %s: %s"


def _load_one(path: Path) -> InterventionMemoryRecord | None:
    """Parse one JSON file into an `InterventionMemoryRecord`.

    Returns `None` on any per-file error and logs a WARNING. Never
    raises — callers MUST be tolerant.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(_WARNING_FORMAT, path.name, type(exc).__name__, str(exc))
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning(_WARNING_FORMAT, path.name, type(exc).__name__, exc.msg)
        return None
    try:
        return InterventionMemoryRecord.model_validate(payload)
    except ValidationError as exc:
        logger.warning(_WARNING_FORMAT, path.name, type(exc).__name__, exc.error_count())
        return None


def _is_existing_dir(path: Path) -> bool:
    """Return True if `path` exists AND is a directory.

    Used to short-circuit glob when the interventions dir is missing so
    NORA never auto-creates the dir.
    """
    return path.is_dir()


def read_records(settings: Any) -> list[InterventionMemoryRecord]:
    """Glob `*.json` under `settings.nora_interventions_dir` and parse each file.

    Returns `[]` if the directory is absent (does NOT create it). Per-file
    errors (corrupt JSON, validation failure, I/O error) log a WARNING
    and skip the file — never raises.
    """
    base_dir: Path = Path(settings.nora_interventions_dir)
    if not _is_existing_dir(base_dir):
        return []

    records: list[InterventionMemoryRecord] = []
    for path in sorted(base_dir.glob("*.json")):
        record = _load_one(path)
        if record is not None:
            records.append(record)
    return records


__all__ = ["read_records", "_load_one", "_is_existing_dir"]