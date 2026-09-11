"""NDJSON rotation for the SessionJournal — R5.

Rotation policy: when the in-memory `trace` exceeds `max_steps`, the
oldest step is displaced (one JSON object per line) to the rotated NDJSON
file at `<journal_dir>/<session_id>.log.ndjson`. The canonical `trace`
stays bounded; the NDJSON file is append-only and is NEVER truncated by
the journal itself.

The rotation is isolated here so its tests don't drag the whole journal
into scope.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from nora.core.session_models import SessionState

logger = logging.getLogger("nora.core.session_rotation")

# Suffix for the rotated NDJSON file. Stable; consumers depend on this.
_NDJSON_SUFFIX: str = ".log.ndjson"

# POSIX file mode for NDJSON files. Best-effort on non-POSIX.
_NDJSON_MODE: int = 0o600
_POSIX: bool = os.name == "posix"


def ndjson_path(journal_dir: Path, session_id: str) -> Path:
    """Return the rotated NDJSON path for `session_id`."""
    return journal_dir / f"{session_id}{_NDJSON_SUFFIX}"


def rotate_if_needed(state: SessionState, ndjson_path: Path, max_steps: int) -> SessionState:
    """If `len(state.trace) > max_steps`, displace the oldest step to NDJSON.

    Returns a new `SessionState` with the displaced step removed from
    `trace`. Returns `state` unchanged when no rotation is needed.

    The function is synchronous and side-effecting on `ndjson_path` only.
    Caller persists the returned state via the atomic JSON write.
    """
    if len(state.trace) <= max_steps:
        return state

    oldest = state.trace[0]
    line = json.dumps(oldest.model_dump(mode="json"), ensure_ascii=False)
    with ndjson_path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())
    if _POSIX:
        # Best-effort: chmod the file (after creation) to 0o600.
        try:
            os.chmod(ndjson_path, _NDJSON_MODE)
        except OSError:  # pragma: no cover - defensive
            pass

    rotated = state.model_copy(deep=True)
    rotated.trace = state.trace[1:]
    logger.info(
        "session journal rotation: displaced step=%d to %s",
        oldest.step,
        ndjson_path,
    )
    return rotated


__all__ = ["rotate_if_needed", "ndjson_path"]
