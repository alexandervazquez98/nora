"""Atomic write logic for the intervention writer (W3).

Pure helpers (no business logic):

- `_atomic_write(target, content)` — `tmp + fsync + os.replace` so a
  SIGKILL between `write_text` and `replace` cannot leave a torn
  `.json` file (the `.tmp` suffix is naturally skipped by the reader's
  `*.json` glob).
- `_sweep_stale_tmp(base_dir, max_age_seconds)` — removes
  `*.json.tmp` files older than `max_age_seconds`. Called at the top
  of `save_intervention_record` to clean up after a prior crash.
- `_ensure_dir(base_dir)` — idempotent mkdir of the interventions dir.

The public entry point `save_intervention_record(settings, payload)`
lives in the same module but is the orchestration layer (validate →
sanitize → build filename → containment → sweep → atomic write).
This module is sync stdlib only (`json`, `os`, `pathlib`) — no
`subprocess`, `shutil`, or network I/O.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


# Stale `.tmp` sweep window: per W3, the writer sweeps `.json.tmp`
# files older than 3600 s on every call. Longer than a typical
# SIGKILL→next-call gap; shorter than a daily retention window.
_TMP_SWEEP_MAX_AGE_SECONDS: int = 3600

# Number of retry attempts on filename collision (per W1's 5-retry
# budget). The retry loop rolls a fresh `secrets.token_hex(3)` each
# pass; on the fifth collision the writer returns `DUPLICATE_INTERVENTION_ID`.
_COLLISION_RETRIES: int = 5


def _ensure_dir(base_dir: Path) -> None:
    """Idempotently create `base_dir`. The reader refuses to do this."""
    base_dir.mkdir(parents=True, exist_ok=True)


def _atomic_write(target: Path, content: str) -> None:
    """Write `content` to `target` atomically via `tmp + fsync + os.replace`.

    Args:
        target: The final `.json` path.
        content: The serialised JSON string.

    Steps:
        1. Write `content` to `<target>.json.tmp`.
        2. `fsync` the tmp file's fileno so the bytes are durable on
           disk before the rename.
        3. `os.replace(tmp, target)` — atomic on POSIX/macOS.

    On SIGKILL between steps 1 and 3, the `.tmp` file is left behind;
    the reader's `*.json` glob skips it (suffix mismatch), and the
    next writer call sweeps it via `_sweep_stale_tmp`.
    """
    tmp = target.with_suffix(".json.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        # Flush Python buffers to the kernel buffer cache, then `fsync`
        # to durable storage so `os.replace` does not race a power loss.
        fd = tmp.open(mode="r", encoding="utf-8")
        try:
            os.fsync(fd.fileno())
        finally:
            fd.close()
        os.replace(str(tmp), str(target))
    except Exception:
        # Best-effort cleanup: if `tmp` exists, remove it so the next
        # sweep does not have to.
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def _sweep_stale_tmp(
    base_dir: Path, *, max_age_seconds: int = _TMP_SWEEP_MAX_AGE_SECONDS
) -> list[str]:
    """Remove `*.json.tmp` files under `base_dir` older than `max_age_seconds`.

    Returns the list of filenames removed (sorted for deterministic
    logs / tests).
    """
    import time

    if not base_dir.is_dir():
        return []
    now = time.time()
    removed: list[str] = []
    for path in sorted(base_dir.glob("*.json.tmp")):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if (now - mtime) > max_age_seconds:
            try:
                path.unlink()
                removed.append(path.name)
            except OSError as exc:
                logger.warning(
                    "intervention_writer: sweep failed to remove %s: %s",
                    path.name,
                    type(exc).__name__,
                )
    return removed


__all__ = [
    "_atomic_write",
    "_sweep_stale_tmp",
    "_ensure_dir",
    "_TMP_SWEEP_MAX_AGE_SECONDS",
    "_COLLISION_RETRIES",
]
