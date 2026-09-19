"""Atomic JSON persistence for completed probe runs — issue #61 PR2.

Mirrors the intervention_writer pattern:

- Filename template: ``PRB-<sector>-<ap_ip>-<unix>-<6hex>.json``
- Strict regex on ``<sector>`` and ``<ap_ip>`` (same character
  classes as ``intervention_writer/filenames.py``).
- Reuses ``_atomic_write`` / ``_sweep_stale_tmp`` / ``_ensure_dir``
  verbatim from ``nora.intervention_writer.atomic``.
- Returns one of: ``OK`` / ``INVALID_INPUT`` / ``INVALID_PAYLOAD`` /
  ``PATH_TRAVERSAL_DETECTED`` / ``WRITE_ERROR``. The MCP layer maps
  the dict to a JSON-RPC response; the library function never
  raises.

The PR2 writer handles filename collisions internally (5-retry
budget) and reports ``WRITE_ERROR`` only when the budget exhausts —
it never returns ``DUPLICATE_PROBE_RUN_ID`` (which would be the
literal mirror of the intervention writer's
``DUPLICATE_INTERVENTION_ID``). This matches the WU-2.4 spec.
"""

from __future__ import annotations

import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from nora.intervention_writer.atomic import (
    _COLLISION_RETRIES,
    _atomic_write,
    _ensure_dir,
    _sweep_stale_tmp,
)
from nora.intervention_writer.filenames import InvalidFilenameComponent

logger = logging.getLogger(__name__)


# Filename-template pieces — parallel to
# ``intervention_writer/filenames.py``. The character classes are
# identical to the intervention writer's so a path-traversal payload
# cannot sneak through either regex.
_SECTOR_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")
_AP_IP_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.:-]+$")


# Return statuses — mirrors the intervention writer's surface.
OK = "OK"
INVALID_INPUT = "INVALID_INPUT"
INVALID_PAYLOAD = "INVALID_PAYLOAD"
PATH_TRAVERSAL_DETECTED = "PATH_TRAVERSAL_DETECTED"
WRITE_ERROR = "WRITE_ERROR"


def _validate_component(field: str, value: Any, pattern: re.Pattern[str]) -> str:
    """Return ``value`` if it matches ``pattern``; else raise ``InvalidFilenameComponent``.

    Mirror of ``intervention_writer/filenames._validate_component``. Same
    defense-in-depth ``..`` substring check on the IP component.
    """
    if not isinstance(value, str):
        raise InvalidFilenameComponent(field, str(value))
    if not pattern.match(value):
        raise InvalidFilenameComponent(field, value)
    # Defense-in-depth: reject `..` substring in the IP component to
    # close the `/foo/../bar` traversal trick the regex alone misses.
    if field == "ap_ip" and ".." in value:
        raise InvalidFilenameComponent(field, value)
    return value


def build_probe_filename(*, sector: str, ap_ip: str, unix: int, hex_suffix: str) -> str:
    """Build ``PRB-<sector>-<ap_ip>-<unix>-<6hex>.json`` strictly.

    Args:
        sector: Operator-friendly sector alias (e.g. ``"norte"``,
            ``"sec-a"``). Character class: ``^[A-Za-z0-9_-]+$``.
        ap_ip: The AP's IPv4 literal (sanitized form acceptable).
            Character class: ``^[A-Za-z0-9_.:-]+$``; ``..`` rejected.
        unix: 10-digit epoch seconds.
        hex_suffix: 6 random hex chars.

    Raises:
        InvalidFilenameComponent: any component fails its regex. The
            error carries the offending ``field`` so the caller can
            surface a precise ``INVALID_INPUT`` response.
    """
    s = _validate_component("sector", sector, _SECTOR_RE)
    ip = _validate_component("ap_ip", ap_ip, _AP_IP_RE)
    return f"PRB-{s}-{ip}-{unix}-{hex_suffix}.json"


class ProbeRunPayload(BaseModel):
    """Pydantic model for a probe-run payload before persistence.

    The fields are intentionally loose (``extra='ignore'``) so PR3
    can extend the on-disk JSON with PDF path / Markdown summary
    without breaking older readers. Mirrors the
    ``InterventionMemoryRecord`` pattern.
    """

    model_config = ConfigDict(extra="ignore")

    run_id: str = Field(..., min_length=1, max_length=64)
    sector: str = Field(..., min_length=1, max_length=64)
    device_id: str
    ap_ip: str
    started_at_unix: float
    finished_at_unix: float
    metrics_summary: dict[str, Any]
    verdict: dict[str, Any]


def save_probe_run(
    settings: Any,  # nora.config.Settings — typed loosely so tests can pass dummies
    payload: dict[str, Any],
    *,
    base_dir: Path | None = None,
    sector: str = "nora",
) -> dict[str, Any]:
    """Persist a completed probe run as JSON. Never raises.

    Reuses ``_atomic_write``, ``_sweep_stale_tmp``, ``_ensure_dir``
    from ``nora.intervention_writer.atomic``.

    Args:
        settings: NORA Settings (used to read
            ``nora_probe_results_dir``). Tests may pass ``None`` only
            when ``base_dir`` is supplied.
        payload: The :class:`ProbeRunPayload`-compatible dict. Must
            contain ``run_id``, ``device_id``, ``ap_ip``, ``sector``,
            ``started_at_unix``, ``finished_at_unix``,
            ``metrics_summary``, ``verdict``.
        base_dir: Override the target directory (used by tests).
        sector: The sector alias for the filename (defaults to
            ``"nora"``). Tests override to ``"test-sector"`` so the
            filename pattern is deterministic.

    Returns:
        Dict with keys ``status`` (one of OK / INVALID_INPUT /
        INVALID_PAYLOAD / PATH_TRAVERSAL_DETECTED / WRITE_ERROR) and
        optional ``filename`` (when status == OK), ``path``, ``error``,
        ``field``.
    """
    # Step 1: validate the payload with the Pydantic model. Pydantic's
    # ``errors()`` returns a list of dicts with ``loc`` / ``type`` keys;
    # we forward the list verbatim in the ``INVALID_PAYLOAD`` response
    # so the MCP wrapper can surface field names without parsing.
    try:
        validated = ProbeRunPayload.model_validate(payload)
    except ValidationError as exc:
        return {
            "status": INVALID_PAYLOAD,
            "errors": [
                {"loc": list(err.get("loc", [])), "type": err.get("type", "")}
                for err in exc.errors()
            ],
        }

    # Step 2: resolve the target directory. When `base_dir` is None,
    # we read from `settings.nora_probe_results_dir`. A missing field
    # on the settings object falls through to ``WRITE_ERROR`` so the
    # caller surfaces a typed error rather than an AttributeError.
    target_dir: Path
    if base_dir is not None:
        target_dir = base_dir
    else:
        try:
            target_dir = Path(settings.nora_probe_results_dir)
        except AttributeError:
            return {
                "status": WRITE_ERROR,
                "error": "settings.nora_probe_results_dir is required",
            }

    try:
        _ensure_dir(target_dir)
    except OSError as exc:
        return {"status": WRITE_ERROR, "error": f"mkdir failed: {type(exc).__name__}"}

    # Step 3: sweep stale `.tmp` files. This is a no-op on a fresh
    # directory; the sweep guards against SIGKILL recovery.
    _sweep_stale_tmp(target_dir)

    # Step 4: build the filename + handle collisions. The 5-retry
    # budget mirrors the intervention writer; on exhaustion we
    # return ``WRITE_ERROR`` (per the PR2 spec — we do not expose
    # ``DUPLICATE_PROBE_RUN_ID`` to MCP).
    ap_ip = payload.get("ap_ip", "0.0.0.0")
    if not isinstance(ap_ip, str):
        ap_ip = str(ap_ip)
    unix = int(time.time())
    name: str | None = None
    target: Path | None = None
    for attempt in range(_COLLISION_RETRIES):
        hex_suffix = secrets.token_hex(3)
        try:
            name = build_probe_filename(
                sector=sector,
                ap_ip=ap_ip,
                unix=unix,
                hex_suffix=hex_suffix,
            )
        except InvalidFilenameComponent as exc:
            return {
                "status": INVALID_INPUT,
                "error": str(exc),
                "field": exc.field,
            }
        target = target_dir / name
        # Defense-in-depth path-traversal containment: the resolved
        # target MUST stay under the configured directory.
        try:
            if not target.resolve().is_relative_to(target_dir.resolve()):
                return {"status": PATH_TRAVERSAL_DETECTED}
        except (OSError, ValueError):
            return {"status": PATH_TRAVERSAL_DETECTED}
        if not target.exists():
            break
    else:
        return {"status": WRITE_ERROR, "error": "filename collision after 5 retries"}

    # `name` and `target` are guaranteed set by the loop above when
    # we reach this point (the for-else clause returns early).
    assert name is not None
    assert target is not None

    # Step 5: serialize + atomic write. The dump uses ``mode='json'``
    # so any nested Pydantic models convert to JSON-safe primitives
    # (a ProbeRunCompleted envelope embedded in `verdict` would
    # otherwise raise on the default ``python`` mode).
    try:
        content = json.dumps(validated.model_dump(mode="json"), indent=2, sort_keys=True)
    except (TypeError, ValueError) as exc:
        return {
            "status": INVALID_PAYLOAD,
            "error": f"json encode failed: {type(exc).__name__}",
        }

    try:
        _atomic_write(target, content)
    except OSError as exc:
        return {
            "status": WRITE_ERROR,
            "error": f"write failed: {type(exc).__name__}",
        }

    # Log line — kept compatible with the intervention_writer's
    # `event` shape so a future operator dashboard can filter by
    # ``event='probes.persist.ok'`` uniformly.
    logger.info(
        "probes.persist.ok",
        extra={"event": "probes.persist.ok", "run_id": payload["run_id"], "filename": name},
    )
    return {"status": OK, "filename": name, "path": str(target)}


__all__ = [
    "OK",
    "INVALID_INPUT",
    "INVALID_PAYLOAD",
    "PATH_TRAVERSAL_DETECTED",
    "WRITE_ERROR",
    "ProbeRunPayload",
    "build_probe_filename",
    "save_probe_run",
]
