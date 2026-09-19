"""Tests for the spectrum-sweep tool — issue #62 WU-3.

These tests pin the public contract for the WU-3 rewrite of the
``snmp_run_spectrum_analysis`` MCP tool:

* Real Cambium WHISP-BOX-MIBV2-MIB sweep protocol: SET duration
  (``.220.0``) → SET 8 (arm, ``.221.0``) → SET 1 (start, ``.221.0``)
  → GET-poll ``.221.0`` until idle=0 OR timeout elapses.
* Tier-1 ``operator_confirmed`` gate FIRST (preserved from slice 4).
* Maintenance-window guard SECOND (preserved from slice 4).
* ``SpectrumSweepTimeout`` typed exception on poll timeout (no silent
  partial sweep — the legacy slice-4 ``SpectrumAnalysis`` swallowed
  partial results).
* ``SpectrumSweepResult.scan_outcome == "COMPLETED"`` on success,
  carrying the new typed timeline metadata.
* ``sweep_duration_seconds`` keyword override at call time overrides
  ``Settings.nora_spectrum_sweep_duration_seconds``.
* Client lifecycle: the writable client is ``close()``d on both
  happy path and exception path.

A hermetic ``_FakeWritableSnmpClient`` records every SET (oid,
value) in a list, lets the test inject the sequence of GET responses,
and implements the ``get_oid`` / ``walk`` / ``close`` / ``set``
Protocol surface. ``driver._writable_client_factory`` is wired to
return the fake so the helper routes every wire frame through it.

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real
IPs, hostnames, serials, or credentials.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from nora.config import Settings
from nora.drivers.inventory import Inventory
from nora.drivers.oid_catalog import OidCatalog, OidCatalogRegistry

# ---------------------------------------------------------------------------
# Helpers — hermetic inventory + catalog + fake writable client.
# ---------------------------------------------------------------------------


def _build_inventory(tmp_path: Path) -> Inventory:
    """Hermetic inventory with one v2c AP."""
    payload = {
        "devices": [
            {
                "device_id": "ap-7400-01",
                "vendor": "cambium",
                "model": "pmp450i",
                "firmware": "15.2.1",
                "host": "192.0.2.10",
                "snmp_version": "v2c",
                "community": "change-me-v2c",
            },
        ]
    }
    inv_path = tmp_path / "devices.yaml"
    inv_path.write_text(yaml.safe_dump(payload))
    return Inventory.from_yaml(inv_path)


def _radio_seed_oids() -> dict[str, str]:
    """The six radio-metrics REQUIRED_OIDs."""
    return {
        "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.1.1.0",
        "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.1.2.0",
        "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.1.3.0",
        # Issue #54 (2026-09-19): signalStrengthTx replaced by eirp.
        "eirp": "1.3.6.1.4.1.161.19.3.1.1.4.0",
        "ssr": "1.3.6.1.4.1.161.19.3.1.1.5.0",
        "modulationMode": "1.3.6.1.4.1.161.19.3.1.1.6.0",
    }


def _spectrum_oids() -> dict[str, str]:
    """Real Cambium WHISP-BOX-MIBV2-MIB spectrum-sweep OID names + dotted OIDs.

    Issue #62 (2026-09-19): matches the production catalog after WU-1:
    ``spectrumScanDuration`` = ``whispBoxSpectrumScanDuration``
    (``.220.0``, SET duration in seconds) and ``spectrumScanAction`` =
    ``whispBoxSpectrumScanAction`` (``.221.0``, SET arm=8 / start=1,
    GET-poll until idle=0).
    """
    return {
        "spectrumScanDuration": "1.3.6.1.4.1.161.19.3.3.2.220.0",
        "spectrumScanAction": "1.3.6.1.4.1.161.19.3.3.2.221.0",
    }


def _build_catalog(
    firmware: str = "15.2.1",
    *,
    include_spectrum_oids: bool = True,
    omit_action_oid: bool = False,
) -> OidCatalogRegistry:
    """Catalog registry carrying the radio seed + (optional) spectrum OIDs.

    ``omit_action_oid=True`` strips ``spectrumScanAction`` so the
    catalog-miss path (one of the test cases below) fires the
    LookupError before any wire frame.
    """
    oids: dict[str, str] = {}
    oids.update(_radio_seed_oids())
    if include_spectrum_oids:
        spectrum_oids = _spectrum_oids()
        if omit_action_oid:
            spectrum_oids = {k: v for k, v in spectrum_oids.items() if k != "spectrumScanAction"}
        oids.update(spectrum_oids)
    catalog = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware=firmware,
        oids=oids,
    )
    return OidCatalogRegistry(
        _catalogs_path=Path("."),
        _catalogs={("cambium", "pmp450i", firmware): catalog},
    )


class _FakeWritableSnmpClient:
    """Fake WritableSnmpClient — records SETs and serves a queued GET response.

    The fake honours the WU-2 ``WritableSnmpClient`` Protocol surface
    (``get_oid`` / ``walk`` / ``close`` / ``set``) so the spectrum
    helper treats it identically to a production ``WritableV2CClient``
    / ``WritableV3Client``.

    ``set_calls`` captures every SET frame in order as a list of
    ``(oid, value)`` tuples (mirrors ``WritableV2CClient.set``'s
    contract). ``get_responses`` is a queue the test injects; each
    ``get_oid`` pops the next entry. When the queue is exhausted the
    fake raises ``KeyError`` so the spectrum helper's
    ``_poll_sweep_status`` exception handler collapses to ``-1``
    (timeout sentinel) and the loop times out cleanly.

    ``closed`` flips to True after the helper invokes ``close()`` so
    the lifecycle test can assert it.
    """

    def __init__(
        self,
        get_responses: list[int] | None = None,
        *,
        poll_interval_seconds: float = 0.0,
        repeat_last_response: bool = True,
    ) -> None:
        self._get_responses: list[int] = list(get_responses) if get_responses is not None else [0]
        self.set_calls: list[tuple[str, str | int]] = []
        self.get_calls: list[str] = []
        self.closed: bool = False
        # Honour the polling cadence (default 0 keeps the test fast).
        self._poll_interval_seconds = poll_interval_seconds
        # When the queue is exhausted, repeat the last value rather
        # than raising ``KeyError`` so the timeout-path tests can
        # inject a small finite queue without a million-element list.
        self._repeat_last_response = repeat_last_response
        self._last_response: int = self._get_responses[-1] if self._get_responses else 0

    def get_oid(self, oid: str) -> str | int:
        self.get_calls.append(oid)
        if self._get_responses:
            self._last_response = self._get_responses.pop(0)
        elif not self._repeat_last_response:
            raise KeyError(oid)
        return self._last_response

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        return []

    def set(self, oid: str, value: str | int) -> None:
        self.set_calls.append((oid, value))

    def close(self) -> None:
        self.closed = True


def _build_driver(
    *,
    inventory: Inventory,
    registry: OidCatalogRegistry,
    fake_client: _FakeWritableSnmpClient,
) -> Any:
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver

    return Pmp450iSnmpDriver(
        inventory=inventory,
        catalog_registry=registry,
        client_factory=lambda d: fake_client,  # read-only seam (unused by spectrum)
        writable_client_factory=lambda d: fake_client,
    )


def _settings_no_window() -> Settings:
    """Build a hermetic Settings with no maintenance window enforced."""
    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=0,
    )


# ---------------------------------------------------------------------------
# Named test #1 — happy path: SET sequence + GET-poll to 0 + COMPLETED
# ---------------------------------------------------------------------------


def test_spectrum_happy_path_runs_set_then_poll_returns_completed(
    tmp_path: Path,
) -> None:
    """Spectrum sweep emits SET sequence and returns ``scan_outcome="COMPLETED"``.

    Real Cambium WHISP-BOX-MIBV2-MIB protocol: SET duration → SET 8
    (arm) → SET 1 (start) → GET-poll ``.221.0`` until one of the
    completion sentinels in ``_SWEEP_COMPLETION_STATUSES`` (``{0, 3,
    4}``). Per the operator's physical-hardware verification on
    firmware 25.0.1, the GET-idle success sentinel is ``4``
    (``idleCompleteSpectrumAnalysis``). The fake returns ``5`` once
    (in-progress), then ``4`` (idle, results available) so the helper
    reaches the SUCCESS branch and folds the timeline into a typed
    ``SpectrumSweepResult`` with ``final_status=4``.
    """
    from nora.drivers.snmp_pmp450i.spectrum import (
        SpectrumSweepResult,
        fetch_spectrum,
    )

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    spec_oids = _spectrum_oids()

    # Polled status sequence: 5 (in-progress) → 4 (idle, results
    # available). The fake pops in order so the helper sees 5, sleeps,
    # polls again, gets 4, returns.
    fake = _FakeWritableSnmpClient(get_responses=[5, 4])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
    )

    assert isinstance(result, SpectrumSweepResult)
    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4  # idle-complete (real-hardware sentinel)
    assert result.device_id == "192.0.2.10"
    assert result.sweep_duration_seconds == 15
    # Empty per-bin decoding fields (future slice).
    assert result.ranked_clean_frequencies == []
    assert result.noise_floor_dbm == {}
    # Timeline is ISO-8601 with timezone offset.
    assert "T" in result.scan_started_at
    assert "T" in result.scan_completed_at

    # Three SET frames in order: duration=15, arm=8, start=1.
    assert fake.set_calls == [
        (spec_oids["spectrumScanDuration"], 15),
        (spec_oids["spectrumScanAction"], 8),
        (spec_oids["spectrumScanAction"], 1),
    ], f"Expected the documented SET sequence; got {fake.set_calls!r}"

    # The action OID was GET'd at least once (the fake returned 5 then 4).
    assert all(call == spec_oids["spectrumScanAction"] for call in fake.get_calls), (
        f"All GETs must hit the action OID; got {fake.get_calls!r}"
    )
    assert len(fake.get_calls) >= 2, (
        f"Happy path polls at least twice (5, then 4); got {len(fake.get_calls)} GETs"
    )

    # Client lifecycle: close() invoked exactly once on the happy path.
    assert fake.closed is True, "writable client must be close()'d on the happy path"


# ---------------------------------------------------------------------------
# Named test #2 — timeout path: poll never reaches 0 → SpectrumSweepTimeout
# ---------------------------------------------------------------------------


def test_spectrum_timeout_raises_spectrum_sweep_timeout(tmp_path: Path) -> None:
    """Poll never reaches a completion sentinel within the timeout → typed exception.

    Issue #62 (2026-09-19) decision #6: the helper raises a typed
    ``SpectrumSweepTimeout`` (carrying ``device_id``,
    ``duration_seconds``, ``last_status``) instead of silently
    consuming a partial sweep. Per the operator's physical-hardware
    verification on firmware 25.0.1, ``5`` is
    ``inProgressTimedSpectrumAnalysis`` — NOT in
    ``_SWEEP_COMPLETION_STATUSES`` (``{0, 3, 4}``). The fake keeps
    returning ``5`` so the poll loop exhausts the deadline and raises.
    """
    from nora.drivers.exceptions import SpectrumSweepTimeout
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)

    # Poll interval 0 keeps the test fast. The fake returns 5 every
    # time so the helper exhausts the timeout and raises.
    fake = _FakeWritableSnmpClient(get_responses=[5] * 1000)
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=0,
        # Minimal timeout to keep the test fast. The poll loop sleeps
        # `poll_interval_seconds` between attempts; 0s interval + a 1s
        # timeout still finishes the loop in well under a second.
        nora_spectrum_sweep_timeout_seconds=1,
        nora_spectrum_sweep_poll_interval_seconds=0.0,
    )

    with pytest.raises(SpectrumSweepTimeout) as exc_info:
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            operator_confirmed=True,
        )

    exc = exc_info.value
    assert exc.device_id == "192.0.2.10"
    assert exc.duration_seconds == 15  # Settings default
    assert exc.last_status == 5  # last polled (in-progress) — NOT in {0, 3, 4}
    assert "last_status=5" in str(exc)

    # SET sequence still happened (arm+start succeeded); GET-poll
    # exhausted the timeout; client was close()d on the exception path.
    assert len(fake.set_calls) == 3, (
        f"SET sequence must run before the GET-poll loop; got {fake.set_calls!r}"
    )
    assert fake.closed is True, "writable client must be close()'d on the timeout path"


# ---------------------------------------------------------------------------
# Named test #3 — Tier-1 operator_confirmed gate fires BEFORE any wire frame
# ---------------------------------------------------------------------------


def test_spectrum_operator_confirmed_false_raises_before_any_set(
    tmp_path: Path,
) -> None:
    """``operator_confirmed=False`` raises BEFORE any SET/GET.

    Per issue #43 ADDED requirement "Tier-1 Operator Clearance Gate":
    the gate fires BEFORE the maintenance-window check AND BEFORE
    any wire frame. The fake records no calls when the gate refuses.
    """
    from nora.drivers.exceptions import Tier1ClearanceRequired
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[0])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    with pytest.raises(Tier1ClearanceRequired):
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            operator_confirmed=False,
        )

    # Zero SETs, zero GETs, zero close() — the gate fires FIRST.
    assert fake.set_calls == [], (
        f"spectrum MUST NOT emit SET frames when operator_confirmed=False; got {fake.set_calls!r}"
    )
    assert fake.get_calls == [], (
        f"spectrum MUST NOT emit GET frames when operator_confirmed=False; got {fake.get_calls!r}"
    )


def test_spectrum_operator_confirmed_absent_defaults_false_and_raises(
    tmp_path: Path,
) -> None:
    """Absent ``operator_confirmed`` kwarg → default False → raises."""
    from nora.drivers.exceptions import Tier1ClearanceRequired
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[0])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    with pytest.raises(Tier1ClearanceRequired):
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
        )

    assert fake.set_calls == []
    assert fake.get_calls == []


def test_spectrum_operator_confirmed_true_outside_window_still_refuses(
    tmp_path: Path,
) -> None:
    """``operator_confirmed=True`` OUTSIDE the window still refuses.

    Tier-1 invariant: a confirmed clearance does NOT bypass the
    maintenance-window check. The gate passes, the
    ``MaintenanceWindowViolation`` fires next.
    """
    from nora.drivers.exceptions import MaintenanceWindowViolation
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[0])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)

    settings_past_window = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=60,
        nora_maintenance_window_start_minutes_ago=120,  # window ended 60 min ago
    )

    with pytest.raises(MaintenanceWindowViolation):
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings_past_window,
            operator_confirmed=True,
        )

    # Window check fires BEFORE the catalog resolution / SET frames.
    assert fake.set_calls == []
    assert fake.get_calls == []


# ---------------------------------------------------------------------------
# Named test #4 — maintenance window gate fires BEFORE any wire frame
# ---------------------------------------------------------------------------


def test_spectrum_respects_maintenance_window(tmp_path: Path) -> None:
    """Outside the maintenance window the tool raises ``MaintenanceWindowViolation``."""
    from nora.drivers.exceptions import MaintenanceWindowViolation
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[0])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)

    # Window ended one hour ago — call lands outside.
    settings_past_window = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=60,
        nora_maintenance_window_start_minutes_ago=120,
    )

    with pytest.raises(MaintenanceWindowViolation):
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings_past_window,
            operator_confirmed=True,
        )

    # Zero wire frames emitted.
    assert fake.set_calls == [], (
        f"spectrum MUST NOT emit SET frames outside the maintenance window; got {fake.set_calls!r}"
    )
    assert fake.get_calls == [], (
        f"spectrum MUST NOT emit GET frames outside the maintenance window; got {fake.get_calls!r}"
    )


# ---------------------------------------------------------------------------
# Named test #5 — catalog miss: LookupError on missing spectrumScanAction
# ---------------------------------------------------------------------------


def test_spectrum_catalog_missing_action_oid_raises_lookup_error(tmp_path: Path) -> None:
    """Catalog missing ``spectrumScanAction`` raises ``LookupError``.

    Mirrors the legacy slice-4 helper behaviour: the catalog
    verification gate rejects catalogs missing any of the required
    OID names, but the helper still raises the typed error at the
    call site so operators see which OID is missing.
    """
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    # Strip spectrumScanAction from the catalog.
    registry = _build_catalog(firmware="15.2.1", omit_action_oid=True)
    fake = _FakeWritableSnmpClient(get_responses=[0])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    with pytest.raises(LookupError) as exc_info:
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            operator_confirmed=True,
        )
    assert "spectrumScanAction" in str(exc_info.value)

    # Catalog check fires AFTER the window check but BEFORE any wire
    # frame; zero SETs/GETs emitted.
    assert fake.set_calls == []
    assert fake.get_calls == []


# ---------------------------------------------------------------------------
# Named test #6 — sweep_duration_seconds override beats Settings default
# ---------------------------------------------------------------------------


def test_spectrum_sweep_duration_seconds_override_is_honored(tmp_path: Path) -> None:
    """``sweep_duration_seconds=30`` SETs duration to 30, not the Settings default."""
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    spec_oids = _spectrum_oids()
    fake = _FakeWritableSnmpClient(get_responses=[0])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)

    # Settings default is 15 — the override of 30 wins.
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=0,
        nora_spectrum_sweep_duration_seconds=15,
    )

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        sweep_duration_seconds=30,
    )

    assert result.sweep_duration_seconds == 30
    # First SET carries the override value, not the Settings default.
    assert fake.set_calls[0] == (spec_oids["spectrumScanDuration"], 30), (
        f"Duration SET must carry the override; got {fake.set_calls[0]!r}"
    )


# ---------------------------------------------------------------------------
# Named test #7 — client lifecycle: close() runs on the exception path too
# ---------------------------------------------------------------------------


def test_spectrum_client_closed_on_maintenance_window_violation(
    tmp_path: Path,
) -> None:
    """Client is ``close()``d even when the window check refuses (defensive).

    The window check fires BEFORE the client opens, so this case
    documents that no client was created — the lifecycle invariant
    covers the post-client-open failure paths (timeout, etc.) via the
    other tests in this module.
    """
    from nora.drivers.exceptions import MaintenanceWindowViolation
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[0])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)

    settings_past_window = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=60,
        nora_maintenance_window_start_minutes_ago=120,
    )

    with pytest.raises(MaintenanceWindowViolation):
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings_past_window,
            operator_confirmed=True,
        )

    # Window check fires before any client opens → factory never runs.
    assert fake.set_calls == []
    assert fake.get_calls == []
    assert fake.closed is False  # nothing to close


# ---------------------------------------------------------------------------
# Defensive coverage — typed exception surface for SpectrumSweepTimeout
# ---------------------------------------------------------------------------


def test_spectrum_sweep_timeout_inherits_driver_error() -> None:
    """``SpectrumSweepTimeout`` is a ``DriverError`` subclass.

    Mirrors the ``Tier1ClearanceRequired inherits DriverError``
    contract: a caller writing ``except DriverError`` must catch
    ``SpectrumSweepTimeout`` without a special-case import.
    """
    from nora.drivers.exceptions import (
        DriverError,
        SpectrumSweepTimeout,
    )

    exc = SpectrumSweepTimeout(
        device_id="ap-7400-01",
        duration_seconds=15,
        last_status=1,
    )
    assert isinstance(exc, DriverError)
    assert exc.device_id == "ap-7400-01"
    assert exc.duration_seconds == 15
    assert exc.last_status == 1
    # Message carries every field for the operator-facing audit trail.
    msg = str(exc)
    assert "ap-7400-01" in msg
    assert "15" in msg
    assert "1" in msg


# ---------------------------------------------------------------------------
# Defensive coverage — sweep result is frozen + serialisable
# ---------------------------------------------------------------------------


def test_spectrum_sweep_result_is_frozen_and_serialisable(tmp_path: Path) -> None:
    """``SpectrumSweepResult`` is frozen and ``model_dump(mode='json')`` clean."""
    from nora.drivers.snmp_pmp450i.spectrum import (
        SpectrumSweepResult,
        fetch_spectrum,
    )

    _ = SpectrumSweepResult  # keep the import so the test asserts the public name

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[0])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
    )

    # Frozen model — assignment raises.
    with pytest.raises(Exception):  # noqa: BLE001 — Pydantic frozen raises ValidationError
        result.scan_outcome = "TIMEOUT"  # type: ignore[misc]

    # JSON-serialisable for the MCP tool boundary.
    dumped = result.model_dump(mode="json")
    assert dumped["scan_outcome"] == "COMPLETED"
    assert dumped["final_status"] == 0
    assert dumped["device_id"] == "192.0.2.10"


# ---------------------------------------------------------------------------
# PR #66 review follow-up — completion sentinel set (issue #62 WU-3)
#
# The original WU-3 helper assumed the GET-side completion sentinel
# was 0 (``stopSpectrumAnalysis``). Per the official Cambium
# WHISP-BOX-MIBV2-MIB and the operator's physical-hardware
# verification on firmware 25.0.1, the actual GET-idle sentinels are
# 3 (``idleNoSpectrumAnalysis`` — no results) and 4
# (``idleCompleteSpectrumAnalysis`` — results ready); 0 is the SET
# abort command and 5 is ``inProgressTimedSpectrumAnalysis``. The
# helper now accepts ``last_status in {0, 3, 4}`` defensively (some
# agents return 0 by GET post-completion). These tests pin the
# expanded sentinel set against every documented value.
# ---------------------------------------------------------------------------


def test_sweep_completes_with_status_3_idle_no_results(tmp_path: Path) -> None:
    """GET-poll returns 3 (``idleNoSpectrumAnalysis``) → ``COMPLETED``.

    Operator hardware verification (firmware 25.0.1): the AP can
    complete a sweep without producing usable results; ``.221.0``
    returns 3 in that case. The helper must accept 3 as a completion
    sentinel and surface ``final_status=3`` in the result.
    """
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    # 5 (in-progress) → 3 (idle, no results).
    fake = _FakeWritableSnmpClient(get_responses=[5, 3])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
    )

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 3
    assert result.device_id == "192.0.2.10"
    assert fake.closed is True


def test_sweep_completes_with_status_4_idle_complete(tmp_path: Path) -> None:
    """GET-poll returns 4 (``idleCompleteSpectrumAnalysis``) → ``COMPLETED``.

    Operator hardware verification (firmware 25.0.1): the success
    sentinel for a sweep that produced usable results is 4. This is
    the value the real AP returns after a successful sector sweep.
    """
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    # 5 (in-progress) → 4 (idle, results ready).
    fake = _FakeWritableSnmpClient(get_responses=[5, 4])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
    )

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4
    assert result.device_id == "192.0.2.10"
    assert fake.closed is True


def test_sweep_completes_with_status_0_post_abort(tmp_path: Path) -> None:
    """GET-poll returns 0 (defensive — some agents return 0 by GET post-completion).

    The Cambium MIB declares 0 as the ``stopSpectrumAnalysis`` SET
    command (not a GET-side value), but the operator's firmware
    observation note flags that some agents DO return 0 by GET
    immediately after a sweep settles. The helper accepts 0
    defensively so those agents do not produce false-positive
    ``SpectrumSweepTimeout`` exceptions.
    """
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    # 5 (in-progress) → 0 (defensive completion sentinel).
    fake = _FakeWritableSnmpClient(get_responses=[5, 0])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
    )

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 0
    assert fake.closed is True


def test_sweep_timeout_when_poll_sees_status_5_in_progress(tmp_path: Path) -> None:
    """GET-poll returns 5 (``inProgressTimedSpectrumAnalysis``) repeatedly → timeout.

    5 is NOT in ``_SWEEP_COMPLETION_STATUSES`` (``{0, 3, 4}``); the
    helper must keep polling until the deadline, then raise
    ``SpectrumSweepTimeout`` carrying ``last_status=5``.
    """
    from nora.drivers.exceptions import SpectrumSweepTimeout
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    # 1000 polls of 5 — well over the 1s timeout budget.
    fake = _FakeWritableSnmpClient(get_responses=[5] * 1000)
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=0,
        nora_spectrum_sweep_timeout_seconds=1,
        nora_spectrum_sweep_poll_interval_seconds=0.0,
    )

    with pytest.raises(SpectrumSweepTimeout) as exc_info:
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            operator_confirmed=True,
        )

    assert exc_info.value.last_status == 5
    assert fake.closed is True


def test_sweep_timeout_when_poll_sees_sentinel_minus_1(tmp_path: Path) -> None:
    """GET-poll raises consistently → ``last_status=-1`` (never polled).

    The poll loop's ``except`` clause collapses wire / type errors to
    ``-1`` (the "never polled" sentinel). When the deadline elapses
    with no successful GET, the helper must raise
    ``SpectrumSweepTimeout`` with ``last_status=-1`` so the operator
    sees the wire was never reachable (as opposed to "polled, but
    never completed").
    """
    from nora.drivers.exceptions import SpectrumSweepTimeout
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)

    # Empty queue + ``repeat_last_response=False`` → every GET raises
    # ``KeyError`` → ``last_status`` collapses to ``-1`` every cycle.
    fake = _FakeWritableSnmpClient(get_responses=[], repeat_last_response=False)
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=0,
        nora_spectrum_sweep_timeout_seconds=1,
        nora_spectrum_sweep_poll_interval_seconds=0.0,
    )

    with pytest.raises(SpectrumSweepTimeout) as exc_info:
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            operator_confirmed=True,
        )

    assert exc_info.value.last_status == -1
    assert fake.closed is True


def test_sweep_completion_constants_exported() -> None:
    """``_SWEEP_COMPLETION_STATUSES`` is exported and equals ``{0, 3, 4}``.

    The constant must be importable from the module's public surface
    (added to ``__all__``) so downstream consumers + tests can
    reference the sentinel set without re-declaring it. The frozenset
    equality pins every accepted value (defending against accidental
    additions / removals during future refactors).
    """
    from nora.drivers.snmp_pmp450i.spectrum import _SWEEP_COMPLETION_STATUSES

    assert isinstance(_SWEEP_COMPLETION_STATUSES, frozenset)
    assert _SWEEP_COMPLETION_STATUSES == frozenset({0, 3, 4})


__all__ = [
    "test_spectrum_happy_path_runs_set_then_poll_returns_completed",
    "test_spectrum_timeout_raises_spectrum_sweep_timeout",
    # Tier-1 gate
    "test_spectrum_operator_confirmed_false_raises_before_any_set",
    "test_spectrum_operator_confirmed_absent_defaults_false_and_raises",
    "test_spectrum_operator_confirmed_true_outside_window_still_refuses",
    # Maintenance window
    "test_spectrum_respects_maintenance_window",
    # Catalog miss
    "test_spectrum_catalog_missing_action_oid_raises_lookup_error",
    # Override
    "test_spectrum_sweep_duration_seconds_override_is_honored",
    # Lifecycle
    "test_spectrum_client_closed_on_maintenance_window_violation",
    # Typed exception surface
    "test_spectrum_sweep_timeout_inherits_driver_error",
    # Frozen / serialisable
    "test_spectrum_sweep_result_is_frozen_and_serialisable",
    # PR #66 review follow-up — expanded sentinel set
    "test_sweep_completes_with_status_3_idle_no_results",
    "test_sweep_completes_with_status_4_idle_complete",
    "test_sweep_completes_with_status_0_post_abort",
    "test_sweep_timeout_when_poll_sees_status_5_in_progress",
    "test_sweep_timeout_when_poll_sees_sentinel_minus_1",
    "test_sweep_completion_constants_exported",
]
