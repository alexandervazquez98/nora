"""Atomic JSON write + dir validation for the SessionJournal package.

This module owns the ONLY filesystem mutations in `nora.core.*`:

* `atomic_write_json` — temp-file + `os.replace`, then `chmod 0o600` on POSIX.
* `ensure_journal_dir` — `mkdir(0o700)` with a symlink-escape guard.
* `canonical_path` / `ndjson_path` — pure path resolvers used by
  `session_journal.py`.

The atomic write is the durability primitive that R3 (write-then-return),
R4 (atomic survive concurrent writers), and R18 (0o600 on POSIX) all
depend on. Anything that writes a session file MUST go through here so the
contract stays in one place.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("nora.core.session_paths")

# POSIX-only flag for `os.chmod` semantics. On non-POSIX (e.g. Windows)
# `0o600` is best-effort; the chmod call is skipped to avoid `OSError`.
_POSIX: bool = os.name == "posix"

# Suffix for the temp file written before `os.replace`. Picked so the
# orphan is recognisable in `var/sessions/`.
_TMP_SUFFIX: str = ".tmp"

# Default mode for the journal directory itself (POSIX).
_DIR_MODE: int = 0o700

# Default mode for session files (POSIX).
_FILE_MODE: int = 0o600


def _atomic_write_json_posix(path: Path, payload: dict[str, Any]) -> None:
    """Write `payload` to `path` atomically with POSIX 0o600 mode."""
    tmp = path.with_name(path.name + _TMP_SUFFIX)
    # Write+fsync the temp file before renaming — `os.replace` is atomic
    # on the same filesystem but a crash before close() can still leave
    # a half-written .tmp. Caller tolerates orphan .tmp on next open.
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, _FILE_MODE)
    os.replace(tmp, path)


def _atomic_write_json_fallback(path: Path, payload: dict[str, Any]) -> None:
    """Non-POSIX best-effort atomic write.

    On platforms where `os.chmod(0o600)` is meaningless, we still write
    to a temp file and rename. The on-disk ACLs are whatever the
    filesystem's default is — the spec accepts this (R18 platform guard).
    """
    tmp = path.with_name(path.name + _TMP_SUFFIX)
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
    os.replace(tmp, path)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomically write `payload` to `path`.

    Process:
        1. Open `<path>.tmp` for writing.
        2. `json.dump(...)` the payload, flush + fsync.
        3. `os.chmod(<path>.tmp, 0o600)` (POSIX only).
        4. `os.replace(<path>.tmp, path)` — atomic on POSIX + Windows ≥ 3.3.

    A crash anywhere before step 4 leaves the previous canonical file
    untouched (R4-S1). A crash after step 4 leaves the new content. Either
    way the canonical file is parseable JSON.
    """
    if _POSIX:
        _atomic_write_json_posix(path, payload)
    else:  # pragma: no cover - non-POSIX path is platform-guarded
        _atomic_write_json_fallback(path, payload)


def ensure_journal_dir(path: Path) -> Path:
    """Create the journal directory if missing, refusing symlink escape.

    Refuses if the FINAL component of `path` is a symlink whose resolution
    lands OUTSIDE `path.parent` — guards against path-injection via
    `NORA_SESSION_JOURNAL_DIR` (e.g. an attacker pre-creates a symlink that
    redirects writes to `/var/log/system`). On POSIX the directory is
    created with mode 0o700.
    """
    # Refuse the FINAL path being a symlink that escapes its parent.
    if path.is_symlink():
        real_target = path.resolve(strict=False)
        try:
            real_target.relative_to(path.parent.resolve(strict=False))
        except ValueError:
            msg = (
                f"Refusing to use symlinked journal dir that escapes parent: "
                f"{path} -> {real_target}"
            )
            raise OSError(msg) from None

    resolved = path.resolve(strict=False)
    # mkdir with 0o700 on POSIX; unspecified on others.
    if _POSIX:
        resolved.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    else:  # pragma: no cover - non-POSIX path is platform-guarded
        resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def canonical_path(journal_dir: Path, session_id: str) -> Path:
    """Return the canonical session file path for `session_id`."""
    return journal_dir / f"{session_id}.json"


def ndjson_path(journal_dir: Path, session_id: str) -> Path:
    """Return the rotated NDJSON path for `session_id`."""
    return journal_dir / f"{session_id}.log.ndjson"


__all__ = [
    "atomic_write_json",
    "ensure_journal_dir",
    "canonical_path",
    "ndjson_path",
]


# Used by tests to opt-in / opt-out of POSIX-only assertions on Windows.
IS_WINDOWS: bool = sys.platform == "win32"
