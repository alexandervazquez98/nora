"""Tests for ``discover_targets`` (PR1 WU-1.2 — issue #61 ICMP probe).

These tests use a hand-rolled ``_FakeDriver`` that satisfies the
``fetch_sm_table`` contract surface (no real SNMP, no real driver
singleton). The fake records keyword arguments so the test can
verify the helper forwards ``device_id`` and ``settings`` unchanged.

Each test runs in <100 ms; no network; no real SNMP. The fake
driver exposes ``_inventory``, ``_catalog_registry``, ``_client_factory``
as no-op ``MagicMock`` instances so the slice-3 helper inside
``driver.fetch_sm_table`` (which reads those three attrs) does not
raise — the fake method short-circuits the call and returns a
pre-built :class:`SubscriberSummary`.

The fixture factory ``make_summary`` builds a ``SubscriberSummary``
from ``(luid, cinr_db)`` tuples so the test setup stays declarative
("ONLINE_ACTIVE LUIDs 1..5", "PRE_EXISTING_OFFLINE LUID 3").
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from nora.drivers.snmp_pmp450i.subscribers import (
    SubscriberRecord,
    SubscriberSummary,
)
from nora.probes.discovery import discover_targets
from nora.probes.models import DiscoveryResult, ProbeTarget

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeDriver:
    """Hand-rolled fake satisfying the contract ``fetch_sm_table`` needs.

    Implements the three attrs ``_inventory``, ``_catalog_registry``,
    ``_client_factory`` as no-op ``MagicMock`` instances with the
    right surfaces, and exposes ``fetch_sm_table`` directly so we
    can record calls and return a pre-built ``SubscriberSummary``.
    """

    def __init__(self, summary: SubscriberSummary) -> None:
        # Three no-op stand-ins (the slice-3 helper reads these attrs
        # but we never call into them — fetch_sm_table is mocked below).
        self._inventory = MagicMock()
        self._catalog_registry = MagicMock()
        self._client_factory = MagicMock()
        self._summary = summary
        self.calls: list[dict[str, Any]] = []

    def fetch_sm_table(self, *, device_id: str, settings: Any | None = None) -> SubscriberSummary:
        self.calls.append({"device_id": device_id, "settings": settings})
        return self._summary


def make_record(
    luid: str, *, cinr_db: int = 22, link_status: str = "inSession"
) -> SubscriberRecord:
    """Build a minimal ``SubscriberRecord`` for the helper's input.

    Defaults match a healthy online SM (``link_status="inSession"``,
    ``cinr_db >= 18``); tests that exercise degraded paths tweak
    these via kwargs.
    """
    return SubscriberRecord(
        luid=luid,
        session_uptime=86400,
        cinr_db=cinr_db,
        link_status=link_status,
        modulation="8X",
    )


def make_summary(
    ap_host: str,
    online: list[tuple[str, int]],
    degraded: list[tuple[str, int]],
    offline: list[tuple[str, int]] | None = None,
) -> SubscriberSummary:
    """Build a :class:`SubscriberSummary` from ``(luid, cinr_db)`` tuples.

    Args:
        ap_host: IPv4 literal the fake puts on ``target_ip``.
        online: ``(luid, cinr_db)`` pairs for the ``ONLINE_ACTIVE`` bucket.
        degraded: ``(luid, cinr_db)`` pairs for the ``ACTIVE_DEGRADED`` bucket.
        offline: ``(luid, cinr_db)`` pairs for the ``PRE_EXISTING_OFFLINE``
            bucket. Defaults to an empty list.
    """
    offline = offline or []
    online_records = [
        make_record(luid, cinr_db=cinr, link_status="inSession") for luid, cinr in online
    ]
    degraded_records = [
        make_record(luid, cinr_db=cinr, link_status="inSession") for luid, cinr in degraded
    ]
    offline_records = [
        make_record(luid, cinr_db=cinr, link_status="idle") for luid, cinr in offline
    ]
    return SubscriberSummary(
        target_ip=ap_host,
        online_active=online_records,
        active_degraded=degraded_records,
        pre_existing_offline=offline_records,
        baseline_size=len(online_records) + len(degraded_records),
        pre_existing_offline_count=len(offline_records),
        fetched_at="2026-09-19T00:00:00+00:00",
    )


def _luids(targets: list[ProbeTarget]) -> list[str]:
    """Extract LUIDs from a list of :class:`ProbeTarget` (SMs only)."""
    return [t.luid for t in targets]


# ---------------------------------------------------------------------------
# Named test #1 — AP first, SMs in LUID-ascending order
# ---------------------------------------------------------------------------


def test_returns_ap_target_first_then_sm_targets_sorted_by_luid() -> None:
    """AP target leads, then ONLINE_ACTIVE + ACTIVE_DEGRADED SMs in LUID order.

    ONLINE_ACTIVE LUIDs are intentionally out of order in the input
    (``[3, 1, 2]``) so the test would fail if the helper preserved
    the input order instead of sorting. ACTIVE_DEGRADED LUIDs
    (``[5, 4]``) are also out of order. The expected SM order is
    ``[1, 2, 3, 4, 5]``.
    """
    summary = make_summary(
        ap_host="192.0.2.1",
        online=[("3", 24), ("1", 22), ("2", 25)],
        degraded=[("5", 12), ("4", 14)],
    )
    driver = _FakeDriver(summary)

    result = discover_targets(driver=driver, device_id="ap-7400-01")

    assert isinstance(result, DiscoveryResult)
    assert len(result.sm_targets) == 5
    assert _luids(result.sm_targets) == ["1", "2", "3", "4", "5"]
    # The AP target leads via the all_targets property.
    all_targets = result.all_targets
    assert len(all_targets) == 6  # AP + 5 SMs
    assert all_targets[0].role == "ap"
    assert all_targets[0].luid is None
    assert all_targets[0].host == "192.0.2.1"
    assert all_targets[0].label == "AP"
    assert all_targets[0].cinr_db is None
    # Every subsequent target is an SM with the expected shape.
    for target in all_targets[1:]:
        assert target.role == "sm"
        assert target.luid is not None
        assert target.label == f"SM {target.luid}"


# ---------------------------------------------------------------------------
# Named test #2 — PRE_EXISTING_OFFLINE excluded from targets
# ---------------------------------------------------------------------------


def test_excludes_pre_existing_offline_subscribers_from_targets() -> None:
    """A PRE_EXISTING_OFFLINE subscriber MUST NOT appear in ``sm_targets``.

    No ``target_luids`` filter is supplied, so ``excluded_luids`` is
    empty (the helper only reports exclusions for LUIDs the operator
    explicitly asked for).
    """
    summary = make_summary(
        ap_host="192.0.2.1",
        online=[("1", 22), ("2", 22)],
        degraded=[],
        offline=[("7", 0)],
    )
    driver = _FakeDriver(summary)

    result = discover_targets(driver=driver, device_id="ap-7400-01")

    assert _luids(result.sm_targets) == ["1", "2"]
    assert "7" not in _luids(result.sm_targets)
    assert result.excluded_luids == []


# ---------------------------------------------------------------------------
# Named test #3 — target_luids filter restricts to the subset
# ---------------------------------------------------------------------------


def test_target_luids_filter_restricts_targets_to_subset() -> None:
    """``target_luids=["2", "4"]`` returns only SMs with those LUIDs.

    The output preserves the helper's LUID-ascending order (2, 4);
    it does NOT preserve the caller's order. ``excluded_luids`` is
    empty because the filter targets were both included in the
    ONLINE_ACTIVE bucket.
    """
    summary = make_summary(
        ap_host="192.0.2.1",
        online=[("1", 22), ("2", 24), ("3", 23), ("4", 25), ("5", 21)],
        degraded=[],
    )
    driver = _FakeDriver(summary)

    result = discover_targets(driver=driver, device_id="ap-7400-01", target_luids=["2", "4"])

    assert _luids(result.sm_targets) == ["2", "4"]
    assert result.excluded_luids == []


# ---------------------------------------------------------------------------
# Named test #4 — operator-requested PRE_EXISTING_OFFLINE LUID reported
# ---------------------------------------------------------------------------


def test_target_luids_filter_reports_offline_luid_in_excluded() -> None:
    """A LUID the operator asks for but lands in PRE_EXISTING_OFFLINE
    is reported in ``excluded_luids`` in the operator's original order.

    The ``excluded_luids`` list preserves the caller's input order so
    the operator gets feedback ("you asked for LUIDs 1, 3; LUID 3 is
    offline and was skipped") rather than an alphabetical surprise.
    """
    summary = make_summary(
        ap_host="192.0.2.1",
        online=[("1", 22), ("2", 22)],
        degraded=[],
        offline=[("3", 0)],
    )
    driver = _FakeDriver(summary)

    # Caller supplies ["1", "3"] — order matters for excluded_luids.
    result = discover_targets(driver=driver, device_id="ap-7400-01", target_luids=["1", "3"])

    assert _luids(result.sm_targets) == ["1"]
    assert result.excluded_luids == ["3"]


# ---------------------------------------------------------------------------
# Named test #5 — empty target_luids == no filter
# ---------------------------------------------------------------------------


def test_empty_target_luids_means_all() -> None:
    """``target_luids=[]`` is treated as "no filter" — every included SM
    comes back, ``excluded_luids`` stays empty.

    The intent is documented in the helper docstring: an empty list is
    indistinguishable from ``None`` so the operator does not need to
    branch on whether they have a filter when calling.
    """
    summary = make_summary(
        ap_host="192.0.2.1",
        online=[("1", 22), ("3", 22)],
        degraded=[("2", 12)],
    )
    driver = _FakeDriver(summary)

    result = discover_targets(driver=driver, device_id="ap-7400-01", target_luids=[])

    assert _luids(result.sm_targets) == ["1", "2", "3"]
    assert result.excluded_luids == []


# ---------------------------------------------------------------------------
# Named test #6 — alphabetic LUIDs are handled gracefully
# ---------------------------------------------------------------------------


def test_target_luids_accepts_alphabetic_luid_strings() -> None:
    """Non-numeric LUIDs round-trip through the sort comparator.

    The helper's sort key is ``(0, int(luid))`` for digit LUIDs and
    ``(1, luid)`` for the rest. This guarantees a total order
    (digit LUIDs first, then alphabetic LUIDs in lexicographic order)
    even when the SM table mixes both. The test confirms an alphabetic
    LUID survives a filter call and is returned in the SM list.
    """
    summary = make_summary(
        ap_host="192.0.2.1",
        online=[("alpha", 22), ("1", 24)],
        degraded=[],
    )
    driver = _FakeDriver(summary)

    result = discover_targets(driver=driver, device_id="ap-7400-01", target_luids=["alpha"])

    assert _luids(result.sm_targets) == ["alpha"]
    assert result.excluded_luids == []


# ---------------------------------------------------------------------------
# Named test #7 — settings keyword forwarded verbatim
# ---------------------------------------------------------------------------


def test_driver_settings_keyword_is_forwarded() -> None:
    """``driver.fetch_sm_table`` is called with ``device_id=<id>, settings=<obj>``.

    The settings sentinel is a private ``object()`` instance — its
    identity is what matters (the helper must forward it unchanged),
    not its shape (the slice-3 driver reads it lazily).
    """
    summary = make_summary(ap_host="192.0.2.1", online=[("1", 22)], degraded=[])
    driver = _FakeDriver(summary)
    sentinel_settings = object()  # noqa: PLC0415 — opaque sentinel

    discover_targets(driver=driver, device_id="ap-7400-01", settings=sentinel_settings)

    assert len(driver.calls) == 1
    call = driver.calls[0]
    assert call["device_id"] == "ap-7400-01"
    assert call["settings"] is sentinel_settings


# ---------------------------------------------------------------------------
# Named test #8 — AP host/label are the canonical literals
# ---------------------------------------------------------------------------


def test_ap_label_is_always_AP_and_ap_host_is_summary_target_ip() -> None:
    """``ap_label`` is the constant ``"AP"``; ``ap_host`` mirrors the
    upstream ``SubscriberSummary.target_ip`` (the AP's IP literal).

    This pins the slice-1 surface so the coordinator in WU-1.3 can
    rely on ``ap_label == "AP"`` for the ΔRTT/ΔJitter baseline UI.
    """
    summary = make_summary(
        ap_host="192.0.2.42",
        online=[("1", 22)],
        degraded=[],
    )
    driver = _FakeDriver(summary)

    result = discover_targets(driver=driver, device_id="ap-7400-01")

    assert result.ap_label == "AP"
    assert result.ap_host == "192.0.2.42"
    # The AP target in all_targets mirrors the same literals.
    assert result.all_targets[0].host == "192.0.2.42"
    assert result.all_targets[0].label == "AP"


# ---------------------------------------------------------------------------
# Named test #9 — DiscoveryResult is frozen
# ---------------------------------------------------------------------------


def test_discovery_result_is_frozen() -> None:
    """A mutation attempt on a :class:`DiscoveryResult` raises ``ValidationError``.

    Pydantic v2 frozen models reject attribute writes with a
    ``ValidationError`` (not an ``AttributeError``). The test pins
    that contract so the coordinator never relies on accidental
    mutability.
    """
    summary = make_summary(ap_host="192.0.2.1", online=[("1", 22)], degraded=[])
    driver = _FakeDriver(summary)

    result = discover_targets(driver=driver, device_id="ap-7400-01")

    with pytest.raises(ValidationError):
        # ``model_copy(update=...)`` would silently succeed; direct
        # attribute write is the mutation surface Pydantic freezes.
        result.ap_host = "198.51.100.1"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Named test #10 — ProbeTarget is frozen
# ---------------------------------------------------------------------------


def test_probe_target_is_frozen() -> None:
    """A mutation attempt on a :class:`ProbeTarget` raises ``ValidationError``."""
    summary = make_summary(ap_host="192.0.2.1", online=[("1", 22)], degraded=[])
    driver = _FakeDriver(summary)

    result = discover_targets(driver=driver, device_id="ap-7400-01")
    target = result.sm_targets[0]

    with pytest.raises(ValidationError):
        target.host = "198.51.100.99"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ProbeTargetRole sanity — the literal type guard.
# ---------------------------------------------------------------------------


def test_probe_target_role_literal_is_ap_or_sm() -> None:
    """``ProbeTargetRole`` only accepts the two literal values.

    Pinning the literal type keeps the AP target distinguishable
    from SM targets without leaking implementation details (no
    string matching on ``label``). The test constructs one target of
    each role and asserts the round-trip via ``role``.
    """
    ap = ProbeTarget(role="ap", host="192.0.2.1", label="AP")
    sm = ProbeTarget(role="sm", luid="1", host="1", label="SM 1", cinr_db=22)

    assert ap.role == "ap"
    assert sm.role == "sm"
    # ``cinr_db`` is None for APs by convention.
    assert ap.cinr_db is None
    assert sm.cinr_db == 22
