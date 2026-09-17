"""`Pmp450iDriver` — public facade.

Single read-only surface exposed to the MCP layer. Construction is via
dependency injection: the inventory, the catalog registry, and the
`client_factory` (used to choose v2c vs v3 from the device's
`snmp_version`) are passed in. Tests inject fakes; production wires
the registry into the boot sequence in `cli.py`.

Flow (Driver-R1, R5, R6):

    resolve catalog -> get values for required OIDs -> fold into
    `RadioMetricsReport`. The within-call OID cache ensures repeated
    lookups for the same OID only emit one wire GET.

After the `nora-mcp-thin-split` cut, the driver no longer calls
`nora_session_set_focus` — the SessionJournal was eliminated from the
codebase entirely (locked decision 3 + 5).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any, Callable, Protocol

from packaging.version import InvalidVersion, Version

from nora.drivers.exceptions import (
    NetworkUnreachableError,
    SnmpTimeoutError,
)
from nora.drivers.inventory import Device
from nora.drivers.mutable_inventory import _InventoryLike
from nora.drivers.oid_catalog import REQUIRED_OIDS, OidCatalog, OidCatalogRegistry
from nora.drivers.snmp_pmp450i.client import SnmpClient
from nora.drivers.snmp_pmp450i.report import RadioMetricsReport

logger = logging.getLogger("nora.drivers.snmp_pmp450i")

# RFC 1213 sysDescr — the agent's "human-readable" identification string.
# Cambium firmware advertises the version somewhere inside this string;
# the parser below extracts the first semver-shaped token it finds.
_SYSDESCR_OID: str = "1.3.6.1.2.1.1.1.0"
_FIRMWARE_RE: re.Pattern[str] = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


class _ClientFactory(Protocol):
    """Factory used to construct a client for `device`.

    Tests inject a callable returning a mock client; production passes
    a factory that picks `V2CClient` / `V3Client` from the device's
    `snmp_version`.
    """

    def __call__(self, device: Device) -> SnmpClient: ...


def default_client_factory(device: Device) -> SnmpClient:
    """Pick v2c or v3 from `device.snmp_version`."""
    from nora.drivers.snmp_pmp450i.v2c import make_v2c_client
    from nora.drivers.snmp_pmp450i.v3 import make_v3_client

    if device.snmp_version == "v2c":
        return make_v2c_client(device)
    return make_v3_client(device)


class Pmp450iDriver:
    """Public facade over the PMP 450i driver layer.

    Single `@mcp.tool` (`snmp_get_pmp450i_radio_metrics`) talks to one
    instance of this class — `DriverRegistry` provides the module-level
    singleton once `__main__.py` wires it at boot.
    """

    def __init__(
        self,
        *,
        inventory: _InventoryLike,
        catalog_registry: OidCatalogRegistry,
        client_factory: Callable[[Device], SnmpClient] = default_client_factory,
    ) -> None:
        # `_InventoryLike` is the seam (issue #42 / Task 3): both the
        # frozen `Inventory` (back-compat for the 8 read sites) AND
        # `MutableInventory` (Task 3's runtime-mutable seam for
        # `register_device`) satisfy the Protocol. The eight
        # `self._inventory.get(...)` call sites route through whichever
        # facade the boot sequence wired in; for production it is
        # `MutableInventory(base=Inventory.from_yaml(...))`.
        self._inventory = inventory
        self._catalog_registry = catalog_registry
        self._client_factory = client_factory

    # ------------------------------------------------------------------
    # Runtime mutation — `register_device` MCP tool seam (issue #42).
    # ------------------------------------------------------------------

    def register(self, device: Device) -> None:
        """Insert ``device`` into the runtime inventory.

        Issue #42 / change `2026-09-15-register-device-mcp`: the
        `register_device` MCP tool needs a single facade to mutate the
        inventory. The driver exposes ``register(device)`` here so the
        tool body in ``server.py`` calls ``driver.register(...)``
        without reaching into the private ``_inventory`` attribute.

        On a frozen `Inventory` (back-compat path) the call raises
        `AttributeError` because ``Inventory`` is Pydantic-frozen —
        ``cli.main()`` always wires a `MutableInventory` in production
        so this surface is the boot contract.
        """
        self._inventory.register(device)  # type: ignore[attr-defined]

    def unregister(self, device_id: str) -> None:
        """Remove ``device_id`` from the runtime inventory.

        Counterpart to :meth:`register` — same wiring story.
        """
        self._inventory.unregister(device_id)  # type: ignore[attr-defined]

    def fetch_radio_metrics(self, device_id: str) -> RadioMetricsReport:
        """Fetch + fold a typed `RadioMetricsReport` for `device_id`.

        Steps:

        1. `Inventory.get(device_id)` — `DeviceNotFoundError` on miss.
        2. Resolve the catalog for `(vendor, model, firmware)` — the
           catalog registry was verified at boot, so this is a pure
           lookup.
        3. Open a client (v2c or v3) and fetch one value per required
           OID. A within-call LRU cache deduplicates identical OIDs
           (`Driver-R6`).
        4. `RadioMetricsReport.fold` produces the typed return.
        """
        device = self._inventory.get(device_id)
        catalog = self._catalog_registry.resolve((device.vendor, device.model, device.firmware))

        client = self._client_factory(device)
        try:
            values = self._fetch_all(client, catalog, device)
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover - close is best-effort
                pass

        report = RadioMetricsReport.fold(
            device=device,
            catalog=catalog,
            values=values,
            fetched_at=datetime.now(timezone.utc),
        )
        return report

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _fetch_all(
        self, client: SnmpClient, catalog: OidCatalog, device: Device
    ) -> "dict[str, str | int]":
        """Fetch every required OID exactly once (within this call).

        Wire-level failures are remapped to typed driver exceptions:

        * `TimeoutError`                               -> `SnmpTimeoutError`
        * `ConnectionRefusedError` / `OSError`        -> `NetworkUnreachableError`
        * `ValueError` (parse failure)                 -> `NetworkUnreachableError`
        """

        @lru_cache(maxsize=None)
        def _cached(oid: str) -> "str | int":
            return client.get_oid(oid)

        target = f"{device.host}:{device.port}"
        values: "dict[str, str | int]" = {}
        for oid_name in REQUIRED_OIDS:
            oid = catalog.oids[oid_name]
            try:
                values[oid] = _cached(oid)
            except TimeoutError as exc:
                raise SnmpTimeoutError(oid) from exc
            except OSError as exc:
                raise NetworkUnreachableError(target) from exc
            except ValueError as exc:
                raise NetworkUnreachableError(f"OID {oid}: {exc}") from exc
        return values


__all__ = [
    "Pmp450iDriver",
    "Pmp450iSnmpDriver",
    "default_client_factory",
]


# ---------------------------------------------------------------------------
# Slice 1 — `Pmp450iSnmpDriver` adapter + `report_firmware()` typed return.
#
# `Pmp450iSnmpDriver` IS-A `Pmp450iDriver` (preserved public surface) AND
# IS-A `DeviceDriverInterface` (the seam). It adds:
#
# * `report_firmware(device_id) -> Version` — slice 1 (the named test
#   `report_firmware_returns_typed_version` pins it).
# * Five `fetch_*` stubs that land in slices 2/3 — each raises
#   `NotImplementedError` referencing the slice that completes it.
#
# Subclassing `Pmp450iDriver` keeps the original fetch path byte-identical
# (the 39 back-compat tests in `tests/test_driver_*` stay green).
# ---------------------------------------------------------------------------


def _parse_sysdescr_version(sys_descr: str) -> Version:
    """Return the first semver-shaped token found in `sys_descr`.

    Cambium's sysDescr strings vary by firmware release — e.g.
    ``"Cambium Networks PMP 450i Access Point. Software Version 15.3.0 build 1"``
    or ``"PMP 450i AP, 15.2.1"``. The regex pulls the first
    ``<digits>.<digits>[.<digits>]`` token; ``Version(...)`` then parses
    it. Raises ``ValueError`` when no version token is present (the
    caller maps it to a typed driver exception).
    """
    match = _FIRMWARE_RE.search(sys_descr)
    if match is None:
        raise ValueError(f"could not parse firmware from sysDescr: {sys_descr!r}")
    try:
        return Version(match.group(1))
    except InvalidVersion as exc:
        raise ValueError(f"invalid firmware token in sysDescr: {sys_descr!r}") from exc


class Pmp450iSnmpDriver(Pmp450iDriver):
    """`DeviceDriverInterface` adapter over the read-only `Pmp450iDriver`.

    Slice 1: ``report_firmware`` is the only new working method. The
    other four ``fetch_*`` methods on the Protocol raise
    ``NotImplementedError`` with a pointer to the slice that completes
    them; their bodies land in PRs 2 and 3 of the chain.

    Subclassing ``Pmp450iDriver`` keeps the legacy
    ``fetch_radio_metrics`` path bit-identical (every back-compat
    test in ``tests/test_driver_*`` stays green).
    """

    # -- slice 1 -----------------------------------------------------------

    def report_firmware(self, device_id: str) -> Version:
        """Query the agent's sysDescr and parse the advertised firmware.

        Steps:

        1. Resolve the device (inventory path; ad-hoc devices land in
           the same call frame).
        2. Open a client and GET ``sysDescr`` (``1.3.6.1.2.1.1.1.0``).
        3. Parse the first semver-shaped token into ``Version``.
        4. WU-1 / PR-44 follow-up — when the inventory is a
           :class:`MutableInventory` AND the resolved device's firmware
           is still the ``(adhoc)`` sentinel, converge the overlay
           entry to the parsed semver via
           :meth:`MutableInventory.update_firmware`. Tier-0 read-side
           callers MUST always succeed; the convergence attempt is
           best-effort and any exception is swallowed (the call still
           returns the typed ``Version``).

        Wire failures keep the existing typed-error mapping from
        ``_call_async`` (``SnmpTimeoutError`` / ``NetworkUnreachableError``).
        """
        device = self._inventory.get(device_id)
        client = self._client_factory(device)
        try:
            raw_value = client.get_oid(_SYSDESCR_OID)
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover - close is best-effort
                pass
        parsed = _parse_sysdescr_version(str(raw_value))

        # WU-1 / PR-44 follow-up — converge `(adhoc)` → parsed semver
        # so subsequent `OidCatalogRegistry.resolve(...)` lookups
        # succeed against the converged triple. Best-effort: a failed
        # write seam MUST NOT break the read-side return value of
        # `report_firmware`. We guard with ``hasattr`` so the
        # `Inventory` (frozen, no `update_firmware`) keeps working
        # unchanged — the convergence only fires when the inventory is
        # the mutable wrapper.
        if device.firmware == "(adhoc)" and hasattr(self._inventory, "update_firmware"):
            try:
                self._inventory.update_firmware(device_id, str(parsed))
            except Exception:  # pragma: no cover - best-effort write seam
                # Tier-0 must always return the parsed `Version`; the
                # operator can re-run `report_firmware` if the overlay
                # update failed for any reason. Logging is left to the
                # write-seam implementation so we do not double-emit.
                pass

        return parsed

    # -- slice 2 ----------------------------------------------------------

    def fetch_ap_summary(self, device_id: str) -> Any:
        """Return a typed ``ApSummary`` for ``device_id``.

        Slice 2 implementation: delegates to ``summaries.fetch_ap_summary``
        which resolves the catalog, opens a client, fetches one wire
        GET per AP-summary OID name, and folds the response into a
        typed Pydantic model. Missing fields become ``None`` (with a
        literal ``"OID catalog fallback: ..."`` warning); wire
        failures surface as typed driver exceptions via the Protocol
        seam.

        Per `pmp450i-radio-tools/spec.md` sub-cluster 1 requirement
        "Both tools SHALL ... MUST call ``OidCatalogRegistry.resolve``
        before any wire frame".
        """
        from nora.drivers.snmp_pmp450i.summaries import fetch_ap_summary

        return fetch_ap_summary(driver=self, device_id=device_id)

    def fetch_frame_utilization(self, device_id: str) -> Any:
        """Return a typed ``FrameUtilization`` for ``device_id``.

        Slice 2 implementation: delegates to ``summaries.fetch_frame_utilization``
        which resolves the catalog, opens a client, fetches one wire
        GET per frame-utilisation OID name, and folds the response into
        a typed Pydantic model.

        Per `pmp450i-radio-tools/spec.md` sub-cluster 1 requirement
        "Both tools SHALL ... MUST call ``OidCatalogRegistry.resolve``
        before any wire frame".
        """
        from nora.drivers.snmp_pmp450i.summaries import fetch_frame_utilization

        return fetch_frame_utilization(driver=self, device_id=device_id)

    # -- slice 3 ----------------------------------------------------------

    def fetch_sm_table(self, device_id: str) -> Any:
        """Return a typed ``SubscriberSummary`` for ``device_id``.

        Slice 3 implementation: delegates to ``subscribers.fetch_sm_table``
        which resolves the catalog, opens a client, walks the SM-table
        subtree, cross-checks against the PRE_DIAGNOSTIC intervention
        history, and folds the response into a typed Pydantic model.

        Per `pmp450i-radio-tools/spec.md` sub-cluster 2 requirement
        "ONE Source Of Truth": ``categorize_subscribers`` is the only
        classification entry point — no inline classification in this
        wrapper.
        """
        from nora.drivers.snmp_pmp450i.subscribers import fetch_sm_table

        return fetch_sm_table(driver=self, device_id=device_id)

    def fetch_sm_detailed_diagnostics(self, device_id: str, *, luid: str) -> Any:
        """Return a typed ``SmDetailedDiagnostics`` for one LUID.

        Slice 3 implementation: delegates to
        ``subscribers.fetch_sm_detailed_diagnostics`` which resolves the
        catalog, opens a client, fetches one ``get_oid`` per
        SM-diagnostics OID name, and folds the values into a typed
        Pydantic model. Missing fields become ``None`` (with a literal
        ``"OID catalog fallback: ..."`` warning).
        """
        from nora.drivers.snmp_pmp450i.subscribers import (
            fetch_sm_detailed_diagnostics,
        )

        return fetch_sm_detailed_diagnostics(driver=self, device_id=device_id, luid=luid)

    # -- slice 4 ----------------------------------------------------------

    def fetch_spectrum(
        self,
        device_id: str,
        *,
        settings: Any = None,
        operator_confirmed: bool = False,
    ) -> Any:
        """Return a typed ``SpectrumAnalysis`` for ``device_id``.

        Slice 4 implementation: delegates to ``spectrum.fetch_spectrum``
        which checks the Tier-1 operator-clearance gate FIRST, then
        the maintenance-window check, resolves the catalog, opens a
        client, walks the three noise-floor OIDs, and folds the
        response into a typed Pydantic model.

        Per `pmp450i-radio-tools/spec.md` sub-cluster 3 requirement
        "snmp_run_spectrum_analysis — Ranked Clean Frequencies +
        Maintenance Window" and the issue #43 ADDED requirement
        "Tier-1 Operator Clearance Gate" — ``operator_confirmed``
        defaults to ``False`` (fail-closed). Tier-1 tools may be
        actively disruptive, so the server-side gate enforces
        explicit operator clearance before any wire frame.
        """
        from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

        return fetch_spectrum(
            driver=self,
            device_id=device_id,
            settings=settings,
            operator_confirmed=operator_confirmed,
        )

    def fetch_migrate(
        self,
        device_id: str,
        *,
        approval_token: str | None,
        target_frequency_mhz: float,
        settings: Any = None,
    ) -> Any:
        """Run the HITL-gated RF migration for ``device_id``.

        Slice 4 implementation: delegates to ``migrate.fetch_migrate``
        which calls :func:`nora.hitl.tokens.verify_approval_token`
        BEFORE any wire frame, follows the make-before-break order
        (ONLINE_ACTIVE → ACTIVE_DEGRADED → AP carrier), and arms a
        ``threading.Timer`` watchdog that reverts the SET frame on
        loss-of-management.

        Per `pmp450i-radio-tools/spec.md` sub-cluster 3 requirements
        "Approval Token Contract", "Rollback Watchdog With Timeout",
        "Make-Before-Break Order + PRE_EXISTING_OFFLINE Exclusion",
        and "Intervention Record Emission On Migration Completion".
        """
        from nora.drivers.snmp_pmp450i.migrate import fetch_migrate

        return fetch_migrate(
            driver=self,
            device_id=device_id,
            approval_token=approval_token,
            target_frequency_mhz=target_frequency_mhz,
            settings=settings,
        )
