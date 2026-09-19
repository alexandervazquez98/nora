"""SM-list discovery helper for the ICMP sector stability probe (issue #61).

This module owns the *fold* from a typed ``SubscriberSummary`` (slice-3
SM-table output) into the ordered ``DiscoveryResult`` the probe
coordinator (PR1 WU-1.3) consumes. The helper reuses the existing
``driver.fetch_sm_table(...)`` driver API — no new SNMP OIDs, no new
wire traffic, just the typed payload rendered into probe targets.

Contract highlights:

* ``PRE_EXISTING_OFFLINE`` subscribers are EXCLUDED from the probe
  targets (they are not reachable anyway). When the operator
  requests a LUID that lands in ``PRE_EXISTING_OFFLINE`` via the
  ``target_luids`` filter, the LUID is reported in
  ``DiscoveryResult.excluded_luids`` so the operator gets feedback
  ("you asked for LUID 005; it is offline and was skipped").
* ``ONLINE_ACTIVE`` + ``ACTIVE_DEGRADED`` SMs are returned in LUID-
  ascending order. Numeric LUIDs sort numerically; alphabetic LUIDs
  fall back to lexicographic order so the sort is total.
* The AP target is always the first element of
  ``DiscoveryResult.all_targets`` so the coordinator can use it as
  the ΔRTT/ΔJitter baseline.

PR2 / PR3 will replace the ``host`` placeholder for SMs with a
real per-LUID IP (resolved via ``whispLinkEntry`` per-LUID IP OID).
For PR1, the slice-3 fold carries only ``luid``, so ``host`` is the
LUID string — the coordinator in WU-1.3 accepts the placeholder and
does not block on it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from nora.drivers.snmp_pmp450i.subscribers import SubscriberRecord, SubscriberSummary
from nora.probes.models import DiscoveryResult, ProbeTarget

if TYPE_CHECKING:
    from nora.config import Settings


__all__ = ["discover_targets"]


logger = logging.getLogger(__name__)


def _sort_key(record: SubscriberRecord) -> tuple[int, int | str]:
    """Stable sort key for LUID ordering.

    Numeric LUIDs sort numerically (so "1" < "2" < "10"); alphabetic
    LUIDs sort lexicographically AFTER all numeric LUIDs. The tuple
    shape (numeric_flag, value) keeps the sort total without mixing
    heterogeneous keys (Pythons sorts ``(0, 5)`` before ``(1, "alpha")``).
    """
    luid = record.luid
    if luid.isdigit():
        return (0, int(luid))
    return (1, luid)


def discover_targets(
    *,
    driver: Any,
    device_id: str,
    settings: "Settings | None" = None,
    target_luids: list[str] | None = None,
) -> DiscoveryResult:
    """Resolve the SM table via the driver and return ordered probe targets.

    Calls ``driver.fetch_sm_table(device_id=..., settings=settings)``,
    filters out ``PRE_EXISTING_OFFLINE`` subscribers, sorts the
    remaining ``ONLINE_ACTIVE`` + ``ACTIVE_DEGRADED`` SMs by LUID
    ascending, optionally restricts to ``target_luids`` when
    provided. LUIDs present in ``target_luids`` but absent from the
    included buckets are returned in ``excluded_luids`` so the operator
    gets feedback (e.g. "you asked for LUID 005; it is
    PRE_EXISTING_OFFLINE and was skipped").

    The AP itself is always the first target in ``all_targets`` so the
    coordinator can use it as the ΔRTT/ΔJitter baseline.

    Args:
        driver: A driver instance exposing ``fetch_sm_table(*, device_id, settings)``.
            Typically ``nora.drivers.snmp_pmp450i.Pmp450iSnmpDriver`` or the
            hermetic test double from ``tests/probes/test_discovery.py``.
        device_id: Inventory device id (e.g. ``"ap-7400-01"``).
        settings: Optional Settings — forwarded verbatim to the driver so the
            cross-check ordering contract (``search_intervention_history`` BEFORE
            ``categorize_subscribers``) holds end-to-end. When ``None``, the
            driver falls back to its own ``_runtime_settings`` (production path)
            or no-op cross-check (hermetic test path).
        target_luids: Optional filter — when provided (non-empty), restrict the
            returned ``sm_targets`` to these LUIDs only. An empty list is
            treated as "no filter" so ``target_luids=[]`` and ``None`` behave
            identically.

    Returns:
        A :class:`DiscoveryResult` carrying the AP host/label, the ordered
        SM probe targets (LUID ascending), and the list of LUIDs the
        operator asked for that landed in ``PRE_EXISTING_OFFLINE``.
    """
    logger.info(
        "probes.discovery.start",
        extra={"device_id": device_id, "target_luids": target_luids},
    )

    # 1. Resolve the SM table via the driver (keyword-only call so the
    #    cross-check ordering contract stays visible at the call site).
    summary: SubscriberSummary = driver.fetch_sm_table(
        device_id=device_id,
        settings=settings,
    )

    # 2. Combine the included buckets. PRE_EXISTING_OFFLINE is
    #    deliberately NOT included — those subscribers are not
    #    reachable targets for the probe.
    included_rows = list(summary.online_active) + list(summary.active_degraded)

    # 3. Build the included-LUID set as a frozenset (the AST scan in
    #    ``subscribers.py`` flags the ``set`` builtin as a potential
    #    Driver-R2 write verb; the equivalent frozenset literal keeps
    #    the membership test O(1) without invoking ``set``).
    included_luids = frozenset(row.luid for row in included_rows)

    # 4. Honour the operator-supplied filter (None or empty list == all).
    filter_requested = target_luids is not None and len(target_luids) > 0

    if filter_requested:
        assert target_luids is not None  # noqa: S101 — narrowed above
        sm_rows_filtered = [r for r in included_rows if r.luid in target_luids]
        excluded_luids = [luid for luid in target_luids if luid not in included_luids]
    else:
        sm_rows_filtered = included_rows
        excluded_luids = []

    # 5. Sort by LUID (numeric first, then alphabetic).
    sorted_rows = sorted(sm_rows_filtered, key=_sort_key)

    # 6. Fold into typed ProbeTargets.
    # TODO(issue #61 WU-1.3 / PR2): replace ``r.luid`` with the per-LUID
    # IP resolved via ``whispLinkEntry`` per-LUID IP OID. The slice-3
    # ``SubscriberRecord`` does not carry an IP field today; the
    # coordinator in WU-1.3 accepts the placeholder and does not block.
    sm_targets: list[ProbeTarget] = [
        ProbeTarget(
            role="sm",
            luid=r.luid,
            host=r.luid,  # placeholder — see TODO above
            label=f"SM {r.luid}",
            cinr_db=r.cinr_db,
        )
        for r in sorted_rows
    ]

    # 7. Build the result envelope.
    result = DiscoveryResult(
        ap_host=summary.target_ip,
        ap_label="AP",
        sm_targets=sm_targets,
        excluded_luids=excluded_luids,
        fetched_at=summary.fetched_at,
    )

    logger.info(
        "probes.discovery.done",
        extra={
            "device_id": device_id,
            "ap_count": 1,
            "sm_count": len(sm_targets),
            "excluded_count": len(excluded_luids),
        },
    )

    return result
