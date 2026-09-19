"""Tests for the spectrum-analysis tool — PR 4 slice 4 commit 2.

These tests pin the public contract for the slice-4
``snmp_run_spectrum_analysis`` MCP tool:

* Ranked clean frequencies — the tool reads N noise-floor OIDs from
  the agent and returns them sorted by ascending noise (lowest
  noise = cleanest first), capped to a configurable top-N.
* Maintenance-window guard — outside an operator-configured window
  the tool MUST raise :class:`MaintenanceWindowViolation` AND emit
  zero wire frames.

The spectrum sweep walks a fixed number of frequencies (synthesised
in the fixture). The driver returns a typed :class:`SpectrumAnalysis`
carrying ``ranked_clean_frequencies`` (kHz), ``noise_floor_dbm``
(freq_kHz -> noise_dbm), and ``scan_started_at``.

Named tests for PR 4 (slice 4 commit 2):

* ``test_spectrum_returns_ranked_clean_frequencies``
* ``test_spectrum_respects_maintenance_window``

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
# Helpers — hermetic inventory + catalog + fake client.
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
    """Spectrum OID names + dotted OIDs (slice 4 PR 2 additions).

    Per Cambium private-enterprise branch
    ``1.3.6.1.4.1.161.19.3.x.x.0``; indices 90-92 to avoid colliding
    with the existing seed (1-11, 50-54, 60, 70-73, 80-83).
    """
    return {
        "spectrumNoiseFloorA": "1.3.6.1.4.1.161.19.3.1.1.90.0",
        "spectrumNoiseFloorB": "1.3.6.1.4.1.161.19.3.1.1.91.0",
        "spectrumNoiseFloorC": "1.3.6.1.4.1.161.19.3.1.1.92.0",
        "spectrumChannelRank": "1.3.6.1.4.1.161.19.3.1.1.93.0",
        "spectrumScanStatus": "1.3.6.1.4.1.161.19.3.1.1.94.0",
    }


def _build_catalog(
    firmware: str = "15.2.1",
    *,
    include_spectrum_oids: bool = True,
) -> OidCatalogRegistry:
    """Catalog registry carrying the radio seed + spectrum OIDs."""
    oids: dict[str, str] = {}
    oids.update(_radio_seed_oids())
    if include_spectrum_oids:
        oids.update(_spectrum_oids())
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


class _FakeSnmpClient:
    """Fake client — returns canned values keyed by dotted OID."""

    def __init__(self, values: dict[str, str | int]) -> None:
        self._values = dict(values)
        self.get_calls: list[str] = []

    def get_oid(self, oid: str) -> str | int:
        self.get_calls.append(oid)
        if oid not in self._values:
            raise KeyError(oid)
        return self._values[oid]

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        return []

    def close(self) -> None:
        return None


def _build_driver(
    *,
    inventory: Inventory,
    registry: OidCatalogRegistry,
    canned: _FakeSnmpClient,
) -> Any:
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver

    return Pmp450iSnmpDriver(
        inventory=inventory,
        catalog_registry=registry,
        client_factory=lambda d: canned,
    )


def _settings_with_window(window_minutes: int) -> Settings:
    """Build a hermetic Settings with a configured maintenance window.

    ``nora_maintenance_window_minutes`` controls the window length;
    ``0`` disables enforcement. The test sets a non-zero value so the
    helper enforces the window boundary.
    """
    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=window_minutes,
    )


# ---------------------------------------------------------------------------
# Named test #1 — spectrum_returns_ranked_clean_frequencies
# ---------------------------------------------------------------------------


def test_spectrum_returns_ranked_clean_frequencies(tmp_path: Path) -> None:
    """Spectrum sweep returns cleanest frequencies first.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 3 requirement
    "snmp_run_spectrum_analysis — Ranked Clean Frequencies + Maintenance
    Window": the tool ranks candidates by lowest measured noise floor.
    The test seeds three noise-floor OIDs with values in random
    order; the returned ``ranked_clean_frequencies`` MUST be sorted
    by ascending noise (lowest noise = cleanest first).
    """
    from nora.drivers.snmp_pmp450i.spectrum import SpectrumAnalysis

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    spec_oids = _spectrum_oids()

    # Three frequencies at 5780, 5800, 5820 MHz (in kHz).
    # Noise floors (dBm) deliberately NOT in ranked order:
    #   5780 MHz → -90 dBm
    #   5800 MHz → -95 dBm  ← cleanest (lowest noise)
    #   5820 MHz → -85 dBm
    canned = _FakeSnmpClient(
        {
            spec_oids["spectrumNoiseFloorA"]: -90,
            spec_oids["spectrumNoiseFloorB"]: -95,
            spec_oids["spectrumNoiseFloorC"]: -85,
            spec_oids["spectrumChannelRank"]: "5780:2,5800:1,5820:3",
            spec_oids["spectrumScanStatus"]: "OK",
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)
    settings = _settings_with_window(0)  # no window enforced

    analysis = driver.fetch_spectrum("ap-7400-01", settings=settings, operator_confirmed=True)

    assert isinstance(analysis, SpectrumAnalysis)
    dumped = analysis.model_dump(mode="json")

    # Ascending noise order: -95 (5800) → -90 (5780) → -85 (5820).
    # Frequencies (kHz): 5_800_000, 5_780_000, 5_820_000.
    assert dumped["ranked_clean_frequencies"] == [5_800_000.0, 5_780_000.0, 5_820_000.0], (
        f"ranked_clean_frequencies MUST be sorted by ascending noise floor; "
        f"got {dumped['ranked_clean_frequencies']}"
    )
    # The noise-floor map carries all three frequencies.
    assert dumped["noise_floor_dbm"] == {
        "5780000.0": -90.0,
        "5800000.0": -95.0,
        "5820000.0": -85.0,
    }


# ---------------------------------------------------------------------------
# Named test #2 — spectrum_respects_maintenance_window
# ---------------------------------------------------------------------------


def test_spectrum_respects_maintenance_window(tmp_path: Path) -> None:
    """Outside the maintenance window the tool raises ``MaintenanceWindowViolation``.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 3 scenario "spectrum
    refuses outside the window": ``now`` outside the configured
    window MUST raise :class:`MaintenanceWindowViolation` AND emit
    zero SNMP frames. The default
    ``nora_maintenance_window_minutes = 0`` means "no window
    enforced" — the test enables a non-zero window so the helper
    enforces the boundary.
    """
    from nora.drivers.exceptions import MaintenanceWindowViolation
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    spec_oids = _spectrum_oids()

    canned = _FakeSnmpClient(
        {
            spec_oids["spectrumNoiseFloorA"]: -90,
            spec_oids["spectrumNoiseFloorB"]: -95,
            spec_oids["spectrumNoiseFloorC"]: -85,
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    # Configure a window that ends ONE HOUR AGO — the call lands
    # outside the window, so the helper raises before any wire GET.
    # The default window start anchor is "now" — shift it backwards
    # so the call is unambiguously outside the window.
    settings_with_past_window = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=60,
        nora_maintenance_window_start_minutes_ago=120,
    )

    with pytest.raises(MaintenanceWindowViolation):
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings_with_past_window,
            operator_confirmed=True,
        )

    # Zero SNMP frames emitted.
    assert canned.get_calls == [], (
        f"spectrum.fetch_spectrum MUST NOT emit wire frames outside the "
        f"maintenance window; got calls {canned.get_calls!r}"
    )


# ---------------------------------------------------------------------------
# Defensive coverage — inside the window proceeds AND emits wire frames.
# ---------------------------------------------------------------------------


def test_spectrum_inside_window_proceeds(tmp_path: Path) -> None:
    """When ``now`` is inside the configured window the tool proceeds.

    Companion test to ``test_spectrum_respects_maintenance_window``:
    the window boundary is honoured on both sides. Anchoring the
    window start in the past and extending the duration so the call
    sits well inside the window gives a deterministic green path.
    """
    from nora.drivers.snmp_pmp450i.spectrum import SpectrumAnalysis

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    spec_oids = _spectrum_oids()

    canned = _FakeSnmpClient(
        {
            spec_oids["spectrumNoiseFloorA"]: -90,
            spec_oids["spectrumNoiseFloorB"]: -95,
            spec_oids["spectrumNoiseFloorC"]: -85,
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=60,
        nora_maintenance_window_start_minutes_ago=10,
    )

    analysis = driver.fetch_spectrum("ap-7400-01", settings=settings, operator_confirmed=True)
    assert isinstance(analysis, SpectrumAnalysis)
    # Wire frames landed — at minimum the three noise-floor OIDs.
    assert len(canned.get_calls) >= 3, (
        f"spectrum MUST emit at least 3 GETs inside the window; got {canned.get_calls!r}"
    )


# ---------------------------------------------------------------------------
# Defensive coverage — exercise the spectrum helpers directly so the
# per-module coverage target (``spectrum.py`` >= 90%) is met.
# ---------------------------------------------------------------------------


def test_spectrum_missing_catalog_oid_raises_lookup_error(tmp_path: Path) -> None:
    """A catalog missing one spectrum OID raises ``LookupError``."""
    from nora.drivers.snmp_pmp450i.spectrum import _resolve_spectrum_oids

    catalog = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        oids={"radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.1.1.0"},
    )
    with pytest.raises(LookupError) as exc_info:
        _resolve_spectrum_oids(catalog)
    assert "spectrumNoiseFloorA" in str(exc_info.value)


def test_spectrum_coerce_int_tolerates_none_and_bad_strings() -> None:
    """``_coerce_int`` collapses ``None`` and unparseable strings to ``0``."""
    from nora.drivers.snmp_pmp450i.spectrum import _coerce_int

    assert _coerce_int(None) == 0
    assert _coerce_int("not-a-number") == 0
    assert _coerce_int("42") == 42
    assert _coerce_int(42) == 42


def test_spectrum_falls_back_to_driver_runtime_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``settings=None`` the driver falls back to its runtime settings.

    Companion defensive coverage: the helper consults
    ``driver._runtime_settings`` when no explicit Settings is
    supplied. The test injects a runtime-settings object carrying a
    short past-anchored window so the path is exercised without
    raising the window-violation exception.
    """
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    spec_oids = _spectrum_oids()
    canned = _FakeSnmpClient(
        {
            spec_oids["spectrumNoiseFloorA"]: -90,
            spec_oids["spectrumNoiseFloorB"]: -95,
            spec_oids["spectrumNoiseFloorC"]: -85,
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    driver._runtime_settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=60,
        nora_maintenance_window_start_minutes_ago=10,
    )

    # No explicit settings — the helper pulls from ``driver._runtime_settings``.
    analysis = fetch_spectrum(driver=driver, device_id="ap-7400-01", operator_confirmed=True)
    assert analysis.ranked_clean_frequencies != []


# ---------------------------------------------------------------------------
# Issue #43 / `2026-09-15-3tier-tool-governance` — Tier-1 operator
# clearance gate. Per `pmp450i-radio-tools/spec.md` ADDED requirement
# "snmp_run_spectrum_analysis Operator Clearance Gate":
#
# * `operator_confirmed=False` (the default, including the
#   absent-parameter case) raises a typed
#   `Tier1ClearanceRequired` BEFORE any SNMP GET is emitted.
# * `operator_confirmed=True` proceeds inside the maintenance
#   window.
# * The gate fires BEFORE the window check (highest-priority invariant).
# ---------------------------------------------------------------------------


def test_spectrum_operator_confirmed_false_raises_before_any_get(
    tmp_path: Path,
) -> None:
    """``operator_confirmed=False`` raises ``Tier1ClearanceRequired`` BEFORE any GET.

    Per `pmp450i-radio-tools/spec.md` ADDED scenario "operator_confirmed=False
    raises BEFORE any SNMP GET" — zero wire frames are sent and the
    maintenance-window check is NOT evaluated.
    """
    from nora.drivers.exceptions import Tier1ClearanceRequired
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    spec_oids = _spectrum_oids()
    canned = _FakeSnmpClient(
        {
            spec_oids["spectrumNoiseFloorA"]: -90,
            spec_oids["spectrumNoiseFloorB"]: -95,
            spec_oids["spectrumNoiseFloorC"]: -85,
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)
    settings = _settings_with_window(0)  # no window enforced

    with pytest.raises(Tier1ClearanceRequired):
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            operator_confirmed=False,
        )

    # Zero wire frames emitted — the gate fires FIRST.
    assert canned.get_calls == [], (
        f"spectrum MUST NOT emit wire frames when operator_confirmed=False; "
        f"got {canned.get_calls!r}"
    )


def test_spectrum_operator_confirmed_true_proceeds_inside_window(
    tmp_path: Path,
) -> None:
    """``operator_confirmed=True`` proceeds inside the maintenance window."""
    from nora.drivers.snmp_pmp450i.spectrum import SpectrumAnalysis, fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    spec_oids = _spectrum_oids()
    canned = _FakeSnmpClient(
        {
            spec_oids["spectrumNoiseFloorA"]: -90,
            spec_oids["spectrumNoiseFloorB"]: -95,
            spec_oids["spectrumNoiseFloorC"]: -85,
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    # Inside the window: window starts 10 min ago, lasts 60 min → "now" sits inside.
    settings_inside = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=60,
        nora_maintenance_window_start_minutes_ago=10,
    )

    analysis = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings_inside,
        operator_confirmed=True,
    )
    assert isinstance(analysis, SpectrumAnalysis)
    assert len(canned.get_calls) >= 3


def test_spectrum_operator_confirmed_absent_defaults_false_and_raises(
    tmp_path: Path,
) -> None:
    """The absent-parameter case resolves to ``False`` and raises.

    Per `pmp450i-radio-tools/spec.md` ADDED scenario "absent parameter
    defaults to False and raises".
    """
    from nora.drivers.exceptions import Tier1ClearanceRequired
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    spec_oids = _spectrum_oids()
    canned = _FakeSnmpClient(
        {
            spec_oids["spectrumNoiseFloorA"]: -90,
            spec_oids["spectrumNoiseFloorB"]: -95,
            spec_oids["spectrumNoiseFloorC"]: -85,
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)
    settings = _settings_with_window(0)

    # No `operator_confirmed` kwarg → default applies → raises.
    with pytest.raises(Tier1ClearanceRequired):
        fetch_spectrum(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
        )

    assert canned.get_calls == [], (
        f"spectrum MUST NOT emit wire frames when operator_confirmed is absent (default False); "
        f"got {canned.get_calls!r}"
    )


def test_spectrum_operator_confirmed_true_outside_window_still_refuses(
    tmp_path: Path,
) -> None:
    """``operator_confirmed=True`` OUTSIDE the window still refuses (no regression).

    Per `pmp450i-radio-tools/spec.md` ADDED scenario "operator_confirmed=True
    OUTSIDE the window still refuses" — the operator-clearance gate
    passes, the existing ``MaintenanceWindowViolation`` invariant still
    fires.
    """
    from nora.drivers.exceptions import MaintenanceWindowViolation
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    spec_oids = _spectrum_oids()
    canned = _FakeSnmpClient(
        {
            spec_oids["spectrumNoiseFloorA"]: -90,
            spec_oids["spectrumNoiseFloorB"]: -95,
            spec_oids["spectrumNoiseFloorC"]: -85,
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

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

    assert canned.get_calls == [], (
        f"spectrum MUST NOT emit wire frames outside the maintenance window; "
        f"got {canned.get_calls!r}"
    )


def test_tier1_clearance_required_inherits_driver_error() -> None:
    """``Tier1ClearanceRequired`` is a ``DriverError`` subclass.

    Per `secure-configuration` scenario "Tier1ClearanceRequired
    inherits DriverError".
    """
    from nora.drivers.exceptions import DriverError, Tier1ClearanceRequired

    exc = Tier1ClearanceRequired(
        tool="snmp_run_spectrum_analysis",
        message="operator clearance required",
    )
    assert isinstance(exc, DriverError)
    assert exc.tool == "snmp_run_spectrum_analysis"
    assert exc.message == "operator clearance required"


__all__ = [
    "test_spectrum_returns_ranked_clean_frequencies",
    "test_spectrum_respects_maintenance_window",
    "test_spectrum_inside_window_proceeds",
    "test_spectrum_missing_catalog_oid_raises_lookup_error",
    "test_spectrum_coerce_int_tolerates_none_and_bad_strings",
    "test_spectrum_falls_back_to_driver_runtime_settings",
    # Issue #43 / Tier-1 gate
    "test_spectrum_operator_confirmed_false_raises_before_any_get",
    "test_spectrum_operator_confirmed_true_proceeds_inside_window",
    "test_spectrum_operator_confirmed_absent_defaults_false_and_raises",
    "test_spectrum_operator_confirmed_true_outside_window_still_refuses",
    "test_tier1_clearance_required_inherits_driver_error",
]
