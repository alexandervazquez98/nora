"""Typed probe-target models for the ICMP sector stability probe (issue #61).

This module owns the *contract* between the SM-list discovery helper
(``src/nora/probes/discovery.py``) and the probe coordinator
(``src/nora/probes/probe.py`` — PR1 WU-1.3). Both consume the
:class:`DiscoveryResult` envelope; nothing else does.

The models are deliberately frozen Pydantic v2 BaseModels so the
coordinator never mutates a target's host/label mid-run. The
``role`` literal keeps the AP target distinguishable from SM
targets without leaking implementation details (no string matching
on ``label``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from nora.probes.diagnostic import SectorVerdict
    from nora.probes.metrics import NodeMetrics
    from nora.probes.sector_delta import SectorDelta

# Role literal — one of two values:
# * ``"ap"`` — the AP itself, used as the ΔRTT/ΔJitter baseline.
# * ``"sm"`` — one subscriber module.
ProbeTargetRole = Literal["ap", "sm"]


class ProbeTarget(BaseModel):
    """One destination for the ICMP probe run (AP or one SM).

    The probe coordinator (PR1 WU-1.3) consumes a list of these in
    order and pings each one at the configured interval. The AP
    target appears first so the coordinator can use it as the
    ΔRTT/ΔJitter baseline for every SM.
    """

    model_config = ConfigDict(frozen=True)

    role: ProbeTargetRole = Field(
        description="Target role: 'ap' for the AP, 'sm' for a subscriber module.",
    )
    luid: str | None = Field(
        default=None,
        description="Subscriber LUID; None when role == 'ap'.",
    )
    host: str = Field(description="IPv4 literal the pinger will send the echo request to.")
    label: str = Field(description="Human-readable label for diagnostics (e.g. 'AP', 'SM 002').")
    cinr_db: int | None = Field(
        default=None,
        description=(
            "SM CINR (dB) from the SM-table fold; only meaningful for SMs. "
            "Always None for the AP target."
        ),
    )


class DiscoveryResult(BaseModel):
    """Return shape of :func:`nora.probes.discovery.discover_targets`.

    The envelope carries the ordered SM probe targets plus a
    convenience list of LUIDs the operator asked for that landed in
    ``PRE_EXISTING_OFFLINE`` (the helper skips them with a note so
    the operator gets feedback instead of a silent drop).
    """

    model_config = ConfigDict(frozen=True)

    ap_host: str = Field(
        description="IPv4 literal of the AP (the ΔRTT/ΔJitter baseline target).",
    )
    ap_label: str = Field(
        description="Human-readable AP label; always 'AP' in the slice-1 surface.",
    )
    sm_targets: list[ProbeTarget] = Field(
        default_factory=list,
        description="Ordered SM targets (LUID ascending, AP excluded).",
    )
    excluded_luids: list[str] = Field(
        default_factory=list,
        description=(
            "LUIDs the operator asked for that landed in PRE_EXISTING_OFFLINE — "
            "returned in the operator's original order so the operator gets "
            "feedback ('you asked for LUID 005; it is offline and was skipped')."
        ),
    )
    fetched_at: str = Field(description="ISO-8601 timestamp from the underlying SubscriberSummary.")

    @property
    def all_targets(self) -> list[ProbeTarget]:
        """The ordered probe list the coordinator (PR1 WU-1.3) consumes.

        The AP target is always first so the coordinator can use it
        as the ΔRTT/ΔJitter baseline for every SM. SMs follow in
        LUID-ascending order.
        """
        ap_target = ProbeTarget(
            role="ap",
            luid=None,
            host=self.ap_host,
            label=self.ap_label,
            cinr_db=None,
        )
        return [ap_target, *self.sm_targets]


class ProbeRunSettings(BaseModel):
    """Snapshot of the configuration used for a run.

    The coordinator (PR1 WU-1.3) freezes the effective duration /
    interval / payload / per-packet timeout into this envelope so the
    MCP wrapper (PR1 WU-1.5) and the PR2 metrics aggregator can log /
    persist the run settings verbatim without re-reading
    :class:`Settings`.
    """

    model_config = ConfigDict(frozen=True)

    duration_seconds: int = Field(
        description=(
            "Effective probe duration for the run, in seconds "
            "(validated against the Settings bounds)."
        ),
    )
    interval_seconds: float = Field(
        description=(
            "Effective inter-packet interval for the run, in seconds "
            "(one packet per destination per interval)."
        ),
    )
    packet_size_bytes: int = Field(
        description="Effective ICMP echo payload size for the run, in bytes.",
    )
    per_packet_timeout_seconds: float = Field(
        description="Effective per-packet wait_for timeout for the run, in seconds.",
    )


class ProbeRunStarted(BaseModel):
    """Snapshot at the moment the coordinator started.

    Captures the run id, the device, the AP baseline target, the
    wall-clock start, and the effective :class:`ProbeRunSettings`.
    The MCP wrapper (PR1 WU-1.5) uses this to surface a "started"
    payload without awaiting the first sample.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(
        description="8 hex chars from secrets.token_hex(4); stable identifier for the run.",
    )
    device_id: str = Field(
        description="Inventory device id (e.g. 'ap-7400-01') the probe is bound to.",
    )
    ap_host: str = Field(
        description="IPv4 literal of the AP (the ΔRTT/ΔJitter baseline target) at run start.",
    )
    started_at_unix: float = Field(
        description="time.time() at the moment the coordinator entered run_probe().",
    )
    settings: ProbeRunSettings = Field(
        description=(
            "Effective configuration for the run "
            "(duration / interval / payload / per-packet timeout)."
        ),
    )


class ProbeRunSummary(BaseModel):
    """Compact summary stored at ``PRB-*.json`` -> ``metrics_summary`` block.

    PR2 land: the summary carries the AP baseline metrics, the per-
    SM metrics list (one per SM, in LUID-ascending order), and the
    sector-level Δ envelope. The model is frozen so PR3 readers can
    rely on a stable JSON shape.
    """

    model_config = ConfigDict(frozen=True)

    ap_metrics: NodeMetrics
    """The AP's per-target metrics (Δ baseline)."""

    sm_metrics: list[NodeMetrics]
    """Per-SM metrics (one NodeMetrics per SM, parallel to ``sector_delta``)."""

    sector_delta: list[SectorDelta]
    """Per-SM Δ vs the AP baseline; parallel to ``sm_metrics``."""


class ProbeRunRecord(BaseModel):
    """One row in the ``icmp_list_probe_runs`` listing — lands at PR3.

    Defined in PR2 so :mod:`nora.probes.persistence` can validate the
    on-disk JSON shape against it. PR3's ``icmp_list_probe_runs`` will
    read ``PRB-*.json`` files and yield ``list[ProbeRunRecord]``.
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    sector: str
    device_id: str
    ap_ip: str
    started_at_unix: float
    finished_at_unix: float
    metrics: ProbeRunSummary
    verdict: SectorVerdict


class ProbeRunCompleted(BaseModel):
    """Local envelope returned by ``analyze_completed_run(state)``.

    PR2's daemon hook attaches this to the run's ``RunState.result_summary``
    field as a serialized dict (using ``.model_dump(mode="json")``), so
    ``icmp_get_sector_stability_progress`` can surface the verdict after
    the run finishes.

    The model is the in-memory canonical envelope; the on-disk shape
    is :class:`ProbeRunRecord` (which only keeps the persisted fields).
    """

    model_config = ConfigDict(frozen=True)

    run_id: str
    metrics: ProbeRunSummary
    verdict: SectorVerdict
    persisted: dict[str, Any]
    """The dict returned by :func:`save_probe_run` (status + filename + path)."""

    analyzed_at_unix: float


__all__ = [
    "DiscoveryResult",
    "ProbeRunCompleted",
    "ProbeRunRecord",
    "ProbeRunSettings",
    "ProbeRunStarted",
    "ProbeRunSummary",
    "ProbeTarget",
    "ProbeTargetRole",
]


# Rebuild the PR2 models so the string annotations above (resolved at
# import time via `from __future__ import annotations`) bind to the
# real classes when ``NodeMetrics`` / ``SectorDelta`` / ``SectorVerdict``
# are imported. The function is idempotent — calling it multiple times
# is a no-op — and it only matters for the forward-reference chain
# (the PR1 models do not need it because they have no forward refs).
def _rebuild_forward_refs() -> None:
    from nora.probes.diagnostic import SectorVerdict
    from nora.probes.metrics import NodeMetrics
    from nora.probes.sector_delta import SectorDelta

    ProbeRunSummary.model_rebuild(
        _types_namespace={"NodeMetrics": NodeMetrics, "SectorDelta": SectorDelta}
    )
    ProbeRunRecord.model_rebuild(
        _types_namespace={
            "NodeMetrics": NodeMetrics,
            "SectorDelta": SectorDelta,
            "SectorVerdict": SectorVerdict,
            "ProbeRunSummary": ProbeRunSummary,
        }
    )
    ProbeRunCompleted.model_rebuild(
        _types_namespace={
            "NodeMetrics": NodeMetrics,
            "SectorDelta": SectorDelta,
            "SectorVerdict": SectorVerdict,
            "ProbeRunSummary": ProbeRunSummary,
        }
    )


_rebuild_forward_refs()
