"""`DeviceDriverInterface` — the vendor-neutral driver seam.

The Protocol is the boundary that lets NORA core talk to any per-vendor
driver without leaking vendor detail (`server.py`, `intervention_*`,
the sanitizer, etc. never import a Cambium type directly).

Six methods live on the Protocol (slice 1 pins the list):

* ``fetch_radio_metrics``         — read-only; pre-slice-1.
* ``fetch_ap_summary``            — slice 2.
* ``fetch_frame_utilization``     — slice 2.
* ``fetch_sm_table``              — slice 3.
* ``fetch_sm_detailed_diagnostics`` — slice 3.
* ``report_firmware``             — slice 1 (PR 1).

The Protocol is ``runtime_checkable`` so the boot sequence can fail-fast
when an adapter doesn't actually implement the seam (the named test
``test_a_missing_method_fails_the_runtime_check`` pins the negative
half).

No MIB prose, no vendor-specific OIDs, no Cambium mention — the seam
is vendor-neutral. Vendor detail stays in
``src/nora/drivers/snmp_pmp450i/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from packaging.version import Version

    from nora.drivers.snmp_pmp450i.report import RadioMetricsReport


@runtime_checkable
class DeviceDriverInterface(Protocol):
    """Vendor-neutral read-only driver seam.

    Every method takes a string ``device_id`` and returns a typed
    scalar / Pydantic model. The Protocol carries NO OIDs, NO MIB
    identifiers, NO vendor names — those live behind the seam.
    """

    def fetch_radio_metrics(self, device_id: str) -> "RadioMetricsReport": ...

    def fetch_ap_summary(self, device_id: str) -> Any: ...

    def fetch_frame_utilization(self, device_id: str) -> Any: ...

    def fetch_sm_table(self, device_id: str) -> Any: ...

    def fetch_sm_detailed_diagnostics(self, device_id: str) -> Any: ...

    def report_firmware(self, device_id: str) -> "Version": ...


__all__ = ["DeviceDriverInterface"]
