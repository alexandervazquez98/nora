"""Public library entry point for the intervention writer.

`save_intervention_record(settings, payload) -> dict` is the canonical
body that the 5th `@mcp.tool` in `src/nora/server.py` delegates to.
The function NEVER raises — every failure mode returns a dict so the
MCP wrapper can surface the error code without parsing a traceback.

Status codes (string in the returned dict's `"status"` field):

- `OK`                                  — record written; payload includes
                                          `"intervention_id"` (filename stem).
- `INVALID_INPUT`                       — filename regex rejected a component.
- `INVALID_PAYLOAD`                     — Pydantic schema validation failed.
- `PATH_TRAVERSAL_DETECTED`             — resolved target escapes the dir.
- `DUPLICATE_INTERVENTION_ID`           — 5-retry collision budget exhausted.
- `WRITE_ERROR`                         — I/O error during atomic write.

Steps (per design.md data flow):

    1. model_validate(payload)         [W5 — schema]
    2. sanitize validated payload       [W4 — defense in depth]
    3. build_filename(payload, ...)     [W1 — regex]
    4. resolve() containment check      [W2 — path-traversal]
    5. sweep stale .tmp                 [W3 — SIGKILL recovery]
    6. tmp + fsync + os.replace         [W3 — atomic write]

This module is sync stdlib only — no `subprocess`, `shutil`, or
network I/O. The 5th `@mcp.tool` (FastMCP) wraps it for stdio/SSE.
"""

from __future__ import annotations

import json
import logging
import secrets
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nora.config import Settings
from nora.intervention_memory.models import InterventionMemoryRecord
from nora.intervention_memory.sanitize import sanitize_record_payload
from nora.intervention_writer.atomic import (
    _COLLISION_RETRIES,
    _TMP_SWEEP_MAX_AGE_SECONDS,
    _atomic_write,
    _ensure_dir,
    _sweep_stale_tmp,
)
from nora.intervention_writer.filenames import InvalidFilenameComponent, build_filename
from nora.sanitizer import Sanitizer

logger = logging.getLogger(__name__)


def _resolved_base(settings: Settings) -> Path:
    """Return the resolved (symlink-followed) interventions directory.

    The writer's containment check calls `Path.resolve()` on the
    candidate filename so symlinks cannot escape the configured dir.
    """
    return Path(settings.nora_interventions_dir).resolve()


def _check_containment(target: Path, base: Path) -> bool:
    """True when `target` resolves to a path under `base`."""
    try:
        resolved = target.resolve()
    except OSError:
        return False
    try:
        return resolved.is_relative_to(base)
    except ValueError:
        return False


def _invalid_input(field: str, value: str) -> dict[str, Any]:
    """Standard `INVALID_INPUT` payload — surfaces the field but not the value.

    The MCP wrapper at `src/nora/server.py` chooses whether to echo
    `value` to the wire; the library function intentionally keeps it
    out of the canonical response so a malformed payload cannot leak
    a typo'd credential through the tool output.
    """
    return {"status": "INVALID_INPUT", "field": field, "error_class": "InvalidFilenameComponent"}


def _invalid_payload(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Standard `INVALID_PAYLOAD` payload — sanitized error list."""
    return {"status": "INVALID_PAYLOAD", "errors": errors}


def _path_traversal() -> dict[str, Any]:
    """Standard `PATH_TRAVERSAL_DETECTED` payload."""
    return {"status": "PATH_TRAVERSAL_DETECTED"}


def _duplicate() -> dict[str, Any]:
    """Standard `DUPLICATE_INTERVENTION_ID` payload (5-retry exhausted)."""
    return {"status": "DUPLICATE_INTERVENTION_ID"}


def _write_error(error_class: str, message: str) -> dict[str, Any]:
    """Standard `WRITE_ERROR` payload."""
    return {
        "status": "WRITE_ERROR",
        "error_class": error_class,
        "message": message,
    }


def save_intervention_record(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    """Atomically write one `InterventionMemoryRecord` JSON to disk.

    Args:
        settings: NORA `Settings` instance (carries `nora_interventions_dir`).
        payload: An MCP payload (raw `dict`) to validate + sanitise + write.

    Returns:
        A dict. On success: `{"status": "OK", "intervention_id": <stem>}`.
        On failure: a dict with `"status"` set to one of:
        `INVALID_INPUT` / `INVALID_PAYLOAD` / `PATH_TRAVERSAL_DETECTED`
        / `DUPLICATE_INTERVENTION_ID` / `WRITE_ERROR`.

    NEVER raises. All exceptions are caught and translated to a status dict.
    """
    # Step 1 — Pydantic schema validation (W5). Catches typo'd
    # `stage`/`status` literals, missing required fields, type drift.
    try:
        record = InterventionMemoryRecord.model_validate(payload)
    except ValidationError as exc:
        # Pydantic's `errors()` returns a list of dicts with field paths
        # — those paths leak schema details (we want to surface them as
        # field names without raw values). Sanitize by stripping `ctx`.
        errors: list[dict[str, Any]] = []
        for err in exc.errors():
            errors.append({"loc": list(err.get("loc", [])), "type": err.get("type", "")})
        return _invalid_payload(errors)

    # Step 2 — Sanitization on write (W4). The on-disk file is
    # self-masked so a future reader that forgets to sanitize still
    # gets a safe file. Uses a per-call `Sanitizer()` so aliases are
    # stable within one call; cross-call stability is not required
    # (the writer's uniqueness is the hex suffix, not the alias map).
    sanitizer = Sanitizer()
    sanitized_dict = sanitize_record_payload(record, sanitizer)

    # Step 3 — Filename build (W1). The regex on ticket/ip rejects
    # path-traversal chars before they reach disk.
    base = _resolved_base(settings)
    try:
        # The filename uses the sanitized timestamp_unix (deterministic;
        # `sanitize_record_payload` does not touch the bypass fields).
        unix = int(record.timestamp_unix)
    except (TypeError, ValueError):
        return _invalid_payload([{"loc": ["timestamp_unix"], "type": "value_error"}])

    # Step 5 — Sweep stale `.tmp` files BEFORE writing. A SIGKILL
    # in a prior call leaves a torn `.tmp` that the reader skips;
    # this sweeps it so the next write doesn't pile up.
    _sweep_stale_tmp(base)

    # Step 4 + 6 — Build filename with collision retries, then atomic
    # write. The retry loop rolls a fresh `secrets.token_hex(3)` per
    # attempt; 5-retry budget per W1.
    _ensure_dir(base)
    last_error: dict[str, Any] | None = None
    for attempt in range(_COLLISION_RETRIES):
        hex_suffix = secrets.token_hex(3)
        try:
            stem = build_filename(
                {
                    "ticket_number": record.ticket_number,
                    "target_ip": record.target_ip,
                },
                unix=unix,
                hex_suffix=hex_suffix,
            )
        except InvalidFilenameComponent as exc:
            return _invalid_input(exc.field, exc.value)

        target = base / stem
        # Path-traversal containment: a symlinked `interventions_dir`
        # whose target is outside the original config is refused.
        # Also catches an attacker who somehow got a filename like
        # `INT-../../../etc/passwd-...` past the regex (defense in
        # depth — the regex should already reject it).
        if not _check_containment(target, base):
            return _path_traversal()

        # Existence check — refuse to overwrite so the caller can
        # retry with a fresh hex suffix. If the target exists and we
        # still have retries, loop and roll a new suffix.
        if target.exists():
            logger.info(
                "intervention_writer: filename collision attempt=%d file=%s",
                attempt + 1,
                stem,
            )
            last_error = _duplicate()
            continue

        # Atomic write — `tmp + fsync + os.replace`. On any OSError,
        # capture and retry (the next hex suffix likely won't collide,
        # but a write error is NOT the same as a collision).
        try:
            content = json.dumps(sanitized_dict, indent=2, sort_keys=True)
            _atomic_write(target, content)
        except OSError as exc:
            return _write_error(type(exc).__name__, str(exc))

        return {"status": "OK", "intervention_id": stem.removesuffix(".json")}

    # Retries exhausted — surface the last collision error.
    return last_error if last_error is not None else _duplicate()


__all__ = [
    "save_intervention_record",
    "_atomic_write",
    "_sweep_stale_tmp",
    "_ensure_dir",
    "_resolved_base",
    "_check_containment",
    "_TMP_SWEEP_MAX_AGE_SECONDS",
    "_COLLISION_RETRIES",
]
