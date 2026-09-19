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

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

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


__all__ = [
    "DiscoveryResult",
    "ProbeTarget",
    "ProbeTargetRole",
]
