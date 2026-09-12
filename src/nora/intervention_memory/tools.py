"""Source-of-truth tool library — powers both MCP wrappers and webui.db shim.

Three library functions:

- `search_intervention_history` — R4 + R7 (keyword I/O cap)
- `get_device_lifecycle_summary` — R5
- `correlate_sector_interference` — R6

These are the canonical bodies. Both the `@mcp.tool` wrappers in
`src/nora/server.py` AND the `Tools` class in `shim_webui.py` delegate
1:1 to these functions, so the two surfaces cannot drift.

The Sanitizer is passed explicitly (not constructed inside) so:
- The MCP wrapper holds one instance for stable aliases across calls.
- Tests inject a fresh instance per test (no alias map leakage).
- `inspect.getsource(Tools)` (used by openchat's deploy script) renders
  the same delegation shape as the MCP wrappers.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from nora.config import Settings
from nora.intervention_memory.correlation import classify_conflict, match_tower
from nora.intervention_memory.models import InterventionMemoryRecord
from nora.intervention_memory.sanitize import sanitize_record_payload
from nora.intervention_memory.storage import read_records
from nora.sanitizer import Sanitizer

logger = logging.getLogger(__name__)

# Status marker for an empty lifecycle (R5-S1). The string is a stable
# contract — `get_device_lifecycle_summary` returns this when no
# records match `target_ip`.
NO_HISTORY_FOUND: str = "NO_HISTORY_FOUND"


# ---------------------------------------------------------------------------
# Search (R4 + R7)
# ---------------------------------------------------------------------------


def _filter_record(
    record: InterventionMemoryRecord,
    *,
    target_ip: Optional[str],
    ticket_number: Optional[str],
    stage: Optional[str],
    keyword: Optional[str],
) -> bool:
    """Return True when `record` matches all provided filters."""
    if target_ip is not None and record.target_ip != target_ip:
        return False
    if ticket_number is not None and ticket_number not in record.ticket_number:
        return False
    if stage is not None and record.stage.lower() != stage.lower():
        return False
    if keyword is not None:
        # Match on the lowered serialised record (substring containment).
        # This mirrors the prototype behaviour exactly.
        serialised = json.dumps(record.model_dump(mode="json")).lower()
        if keyword.lower() not in serialised:
            return False
    return True


def _enforce_keyword_cap(
    records: list[InterventionMemoryRecord],
    *,
    cap: int,
    keyword: Optional[str],
) -> list[InterventionMemoryRecord]:
    """Trim `records` to at most `cap` entries when `keyword` is set.

    Returns the trimmed list. Logs WARNING when the cap kicks in.
    """
    if keyword is None or cap <= 0:
        return records
    if len(records) <= cap:
        return records
    logger.warning(
        "intervention_memory: keyword search cap reached: cap=%d records_read=%d; "
        "further files skipped",
        cap,
        len(records),
    )
    return records[:cap]


def search_intervention_history(
    settings: Settings,
    target_ip: Optional[str] = None,
    ticket_number: Optional[str] = None,
    stage: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 5,
    *,
    sanitizer: Sanitizer,
) -> list[dict[str, Any]]:
    """Search the interventions directory for matching records.

    Filters (apply in this order, AND-combined):
        - `target_ip` (exact equality on `record.target_ip`)
        - `ticket_number` (substring containment on `record.ticket_number`)
        - `stage` (case-insensitive equality)
        - `keyword` (substring containment on `json.dumps(record).lower()`)

    Args:
        settings: NORA `Settings` instance.
        target_ip, ticket_number, stage, keyword: Optional filter args.
        limit: Cap on returned records (default 5).
        sanitizer: Per-session Sanitizer for stable aliases.

    Returns:
        List of sanitized record dicts, sorted by `timestamp_unix` DESC,
        capped to `limit`. Empty list when no records match.
    """
    # R7: keyword cap is enforced at the read boundary so I/O is bounded
    # even when a small `limit` would early-out after filtering.
    cap = settings.nora_interventions_keyword_search_max_records if keyword is not None else None
    records = read_records(settings, limit=cap)
    if keyword is not None and cap is not None and len(records) >= cap:
        # Cap fired — log the warning so an operator can tune `nora_interventions_keyword_search_max_records`.
        logger.warning(
            "intervention_memory: keyword search cap reached: cap=%d records_read=%d; "
            "further files skipped",
            cap,
            len(records),
        )

    matched: list[InterventionMemoryRecord] = []
    for record in records:
        if _filter_record(
            record,
            target_ip=target_ip,
            ticket_number=ticket_number,
            stage=stage,
            keyword=keyword,
        ):
            matched.append(record)

    matched.sort(key=lambda r: r.timestamp_unix, reverse=True)
    sliced = matched[:limit]
    return [sanitize_record_payload(r, sanitizer) for r in sliced]


# ---------------------------------------------------------------------------
# Lifecycle summary (R5)
# ---------------------------------------------------------------------------


def _pick_offline_subscribers(
    records: list[InterventionMemoryRecord],
) -> list[dict[str, Any]]:
    """Return the offline subscriber list from the most recent `PRE_DIAGNOSTIC` record.

    `records` is already sorted DESC by `timestamp_unix`, so the first
    PRE_DIAGNOSTIC in iteration order IS the most recent one. Returns
    `[]` when no PRE_DIAGNOSTIC record exists.
    """
    for record in records:
        if record.stage == "PRE_DIAGNOSTIC":
            return [sub.model_dump(mode="json") for sub in record.network_equipment.pre_existing_offline_subscribers]
    return []


def get_device_lifecycle_summary(
    settings: Settings,
    target_ip: str,
    *,
    sanitizer: Sanitizer,
) -> dict[str, Any]:
    """Compute the per-device lifecycle summary.

    Algorithm:
        1. Search for records matching `target_ip` (limit 20 to give the
           PRE_DIAGNOSTIC extraction enough history).
        2. If empty → return `{"status": NO_HISTORY_FOUND, "target_ip": ...}`.
        3. Otherwise build the 6-field SUCCESS dict.

    Args:
        settings: NORA `Settings` instance.
        target_ip: Required — IP to summarise.
        sanitizer: Per-session Sanitizer.

    Returns:
        Dict with either `status=NO_HISTORY_FOUND` OR the 6 SUCCESS fields:
        `status`, `target_ip`, `total_recorded_interventions`,
        `associated_tickets`, `stages_recorded`, `latest_intervention`,
        `known_pre_existing_offline_subscribers`.
    """
    # `limit=20` gives the PRE_DIAGNOSTIC extraction enough history.
    records_raw = search_intervention_history(
        settings,
        target_ip=target_ip,
        limit=20,
        sanitizer=sanitizer,
    )
    if not records_raw:
        return {"status": NO_HISTORY_FOUND, "target_ip": target_ip}

    # Re-load records for the typed access (PRE_DIAGNOSTIC stage check,
    # pre_existing_offline_subscribers). Doing this through the same
    # search path keeps behaviour identical to what the MCP returns.
    all_records = read_records(settings)
    matched = [r for r in all_records if r.target_ip == target_ip]
    matched.sort(key=lambda r: r.timestamp_unix, reverse=True)

    # Sanitize the latest_intervention via the same path as search.
    latest_payload = sanitize_record_payload(matched[0], sanitizer)

    # `stages_recorded` is the literal stage list in DESC order.
    stages = [r.stage for r in matched]
    tickets = sorted({r.ticket_number for r in matched if r.ticket_number})

    # PRE_DIAGNOSTIC subscriber extraction uses the typed path (not the
    # already-sanitized payload) so the result is a clean list of
    # subscriber dicts; we sanitize once at the end.
    raw_subs = _pick_offline_subscribers(matched)
    sanitized_subs = []
    for sub in raw_subs:
        sanitized_sub = {
            "luid": sub.get("luid"),
            "mac": sanitizer.sanitize(sub.get("mac") or "").text if sub.get("mac") else None,
            "ip": sanitizer.sanitize(sub.get("ip") or "").text if sub.get("ip") else None,
            "uptime": sub.get("uptime"),
            "note": sanitizer.sanitize(sub.get("note") or "").text if sub.get("note") else None,
        }
        sanitized_subs.append(sanitized_sub)

    return {
        "status": "SUCCESS",
        "target_ip": target_ip,
        "total_recorded_interventions": len(matched),
        "associated_tickets": tickets,
        "stages_recorded": stages,
        "latest_intervention": latest_payload,
        "known_pre_existing_offline_subscribers": sanitized_subs,
    }


# ---------------------------------------------------------------------------
# Correlation (R6)
# ---------------------------------------------------------------------------


def correlate_sector_interference(
    settings: Settings,
    tower_name: str,
    target_frequency_mhz: float,
    channel_width_mhz: float = 20.0,
    *,
    sanitizer: Sanitizer,
) -> dict[str, Any]:
    """Find carriers on `tower_name` whose frequency is within `channel_width_mhz` of `target_frequency_mhz`.

    Walks the most recent
    `Settings.nora_interventions_correlate_scan_limit` records. For
    each record with both `network_equipment.system_name` AND
    `network_equipment.carrier_frequency_mhz`:

        1. `match_tower(system_name, tower_name)` substring check.
        2. `abs(carrier - target) < channel_width_mhz` frequency check.
        3. Classify as `CO_CHANNEL` (delta < 0.5) or `ADJACENT_CHANNEL`.

    Args:
        settings: NORA `Settings` instance.
        tower_name: Tower substring to match on `system_name`.
        target_frequency_mhz: Proposed frequency.
        channel_width_mhz: Channel width (default 20.0).
        sanitizer: Per-session Sanitizer.

    Returns:
        Dict with `tower_name`, `proposed_frequency_mhz`,
        `channel_width_mhz`, `is_frequency_clear_on_tower`,
        `detected_conflicts` (sorted by `frequency_delta_mhz` ASC).
    """
    # Cap the scan via the read path so we never load more than the
    # configured number of records.
    cap = settings.nora_interventions_correlate_scan_limit
    records = read_records(settings, limit=cap)
    if len(records) >= cap:
        logger.warning(
            "intervention_memory: correlate scan cap reached: cap=%d records_read=%d",
            cap,
            len(records),
        )
    records.sort(key=lambda r: r.timestamp_unix, reverse=True)

    conflicts: list[dict[str, Any]] = []
    for record in records:
        ne = record.network_equipment
        if ne.system_name is None or ne.carrier_frequency_mhz is None:
            continue
        if not match_tower(ne.system_name, tower_name):
            continue
        carrier = ne.carrier_frequency_mhz
        delta = abs(carrier - target_frequency_mhz)
        if delta >= channel_width_mhz:
            continue
        kind = classify_conflict(carrier, target_frequency_mhz, channel_width_mhz)
        # Sanitize the neighbor_device (system_name) and neighbor_ip (target_ip).
        conflicts.append(
            {
                "neighbor_device": sanitizer.sanitize(ne.system_name).text,
                "neighbor_ip": record.target_ip,  # bypass: target_ip is in the bypass list
                "carrier_frequency_mhz": carrier,
                "frequency_delta_mhz": delta,
                "potential_conflict": kind,
            }
        )

    # Sort by frequency_delta ASC so deterministic / "closest first".
    conflicts.sort(key=lambda c: c["frequency_delta_mhz"])

    return {
        "tower_name": tower_name,
        "proposed_frequency_mhz": target_frequency_mhz,
        "channel_width_mhz": channel_width_mhz,
        "is_frequency_clear_on_tower": len(conflicts) == 0,
        "detected_conflicts": conflicts,
    }


__all__ = [
    "NO_HISTORY_FOUND",
    "search_intervention_history",
    "get_device_lifecycle_summary",
    "correlate_sector_interference",
    "_filter_record",
    "_enforce_keyword_cap",
    "_pick_offline_subscribers",
]