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

import httpx
import pytest
import yaml

import nora.drivers.snmp_pmp450i.spectrum_http as _spectrum_http_module
from nora.config import Settings
from nora.drivers.exceptions import (
    SnmpTimeoutError,
    SpectrumHttpFetchError,
)
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


def _load_fixture_xml() -> str:
    """Load the hermetic Cambium spectrum XML fixture (issue #70 WU-2).

    Reads ``tests/data/fixtures/spectrum/pmp450i_spectrum_sample.xml``
    verbatim so the 8 WU-2 tests share the same fixture as
    ``tests/test_snmp_spectrum_http.py`` (32 bins across 8 unique
    frequencies, cleanest = 3560.0 MHz).
    """
    return (
        Path(__file__).parent / "data" / "fixtures" / "spectrum" / "pmp450i_spectrum_sample.xml"
    ).read_text(encoding="utf-8")


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


class _FakeErrorInjectingSnmpClient(_FakeWritableSnmpClient):
    """Fake that raises ``SnmpTimeoutError`` for the first ``n_error_polls`` GETs.

    PR #66 review follow-up #2: pins the
    ``_poll_sweep_status`` ``except DriverError`` swallow against the
    operator's observed mid-sweep wire failures. Each GET increments a
    counter; while the counter is non-zero, the fake raises
    :class:`SnmpTimeoutError` (a ``DriverError`` subclass) instead of
    popping from ``get_responses``. Once the counter reaches zero the
    fake falls through to the normal queue + ``repeat_last_response``
    behaviour.

    The counter tracks ONLY error polls; regular polls (after the
    counter is zeroed) are not counted.
    """

    def __init__(
        self,
        *args: Any,
        n_error_polls: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._n_error_polls_remaining = n_error_polls

    def get_oid(self, oid: str) -> str | int:
        self.get_calls.append(oid)
        if self._n_error_polls_remaining > 0:
            self._n_error_polls_remaining -= 1
            raise SnmpTimeoutError(oid)
        return super().get_oid(oid)


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


class _FakeUncloseableHttpClient(httpx.Client):
    """``httpx.Client`` subclass whose ``close()`` is a no-op.

    Issue #70 / WU-2: ``fetch_spectrum_xml`` owns the client it
    creates inside its retry loop (``owns_client = http_client is None``)
    and closes it after every attempt. When tests monkey-patch
    ``httpx.Client`` to return a single shared fake, the first
    attempt closes the fake client and subsequent attempts (e.g.
    per-SM GETs in the post-sweep ladder) hit
    ``RuntimeError("Cannot send a request, as the client has been closed.")``.
    Overriding ``close()`` to a no-op keeps the fake reusable across
    the AP + SM ladder steps within a single test invocation.
    """

    def close(self) -> None:
        # Intentionally a no-op so monkey-patched clients survive
        # the ladder's per-attempt close calls.
        return


def _build_fake_http_client(
    *,
    ap_xml: str | None = None,
    ap_error: Exception | None = None,
    sm_xml: dict[str, str] | None = None,
) -> httpx.Client:
    """Build a one-shot ``httpx.Client`` wired to ``httpx.MockTransport``.

    Issue #70 / WU-2: used by the post-sweep ladder tests to keep the
    HTTP fetch hermetic (no real network). The handler recognises
    ``192.0.2.10`` as the AP host and any SM host in ``sm_xml`` as an
    SM. If ``ap_error`` is set, the AP GET raises it on every
    attempt (used by the failure-path tests). If a SM host is
    requested but absent from ``sm_xml``, the SM GET raises
    :class:`SpectrumHttpFetchError` with ``status_code=404`` so the
    caller's non-fatal handler records it as a one-line diagnostic.

    The returned client is an
    :class:`_FakeUncloseableHttpClient` so the ladder's per-attempt
    ``close()`` calls do not invalidate it for the next attempt.
    """
    sm_xml = sm_xml or {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if ap_error is not None and "192.0.2.10" in url:
            raise ap_error
        for sm_host, payload in sm_xml.items():
            if sm_host in url:
                return httpx.Response(200, text=payload)
        if "192.0.2.10" in url:
            if ap_xml is None:
                return httpx.Response(404, text="not found")
            return httpx.Response(200, text=ap_xml)
        raise SpectrumHttpFetchError(
            host=url,
            status_code=404,
            attempts=1,
            message=f"unexpected URL in fake HTTP client: {url}",
        )

    return _FakeUncloseableHttpClient(
        transport=httpx.MockTransport(handler),
        timeout=5.0,
    )


def _settings_no_window() -> Settings:
    """Build a hermetic Settings with no maintenance window enforced.

    Issue #70 / WU-2: the test suite has ``final_status == 4`` paths
    that trigger the post-sweep HTTP ladder. The TEST-NET-1 host
    (``192.0.2.10``) is unreachable from the test runner, so each
    default-configured ladder makes 6 attempts (1 + 5 retries) × 10s
    timeout = ~60s + 5 × 3s inter-attempt delays = ~75s of wall
    time per test. We keep the validation bounds sane (retry delay
    must be ``>= 0.1``, max retries must be ``>= 0``) and pick the
    minimum values the validator accepts. Tests that DEPEND on the
    ladder's behaviour override these (the 8 WU-2 tests inject a
    fake transport or set high retry budgets).
    """
    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=0,
        nora_spectrum_http_timeout_seconds=1.0,
        nora_spectrum_http_max_retries=0,
        nora_spectrum_http_retry_delay_seconds=0.1,
        nora_spectrum_sm_reassociation_timeout_seconds=1.0,
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
        # PR #66 review follow-up #3: the sweep-duration-scaled guard
        # `elapsed >= sweep_duration_seconds` needs `sweep_duration_seconds`
        # to be small in tests so the helper reaches acceptance within a
        # reasonable runtime budget. The happy path uses `sweep_duration_seconds=2`
        # so completion is accepted at `elapsed >= 2.0s`.
        sweep_duration_seconds=2,
    )

    assert isinstance(result, SpectrumSweepResult)
    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4  # idle-complete (real-hardware sentinel)
    assert result.device_id == "192.0.2.10"
    assert result.sweep_duration_seconds == 2  # tool kwarg override (not Settings default)
    # Empty per-bin decoding fields (future slice).
    assert result.ranked_clean_frequencies == []
    assert result.noise_floor_dbm == {}
    # Timeline is ISO-8601 with timezone offset.
    assert "T" in result.scan_started_at
    assert "T" in result.scan_completed_at

    # Three SET frames in order: duration=2 (the override), arm=8, start=1.
    assert fake.set_calls == [
        (spec_oids["spectrumScanDuration"], 2),
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
            # PR #66 review follow-up #3: pass an explicit short sweep
            # duration so the test stays independent of the
            # `nora_spectrum_sweep_duration_seconds` Settings default
            # (which moved 15 → 30). The timeout (1s) fires before the
            # scaled guard would activate anyway, but the override
            # keeps the `exc.duration_seconds` assertion stable.
            sweep_duration_seconds=2,
        )

    exc = exc_info.value
    assert exc.device_id == "192.0.2.10"
    assert exc.duration_seconds == 2  # tool kwarg override (not Settings default)
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
    """Tool-kwarg ``sweep_duration_seconds`` beats the Settings default.

    PR #66 review follow-up #3: the
    ``nora_spectrum_sweep_duration_seconds`` default moved 15 → 30
    (operator's production baseline). The override contract is
    unchanged: the tool kwarg always wins over the Settings default.
    The test uses an explicit override (2) on top of a different
    Settings default (15) so it stays independent of any future
    change to the Settings default value.
    """
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    spec_oids = _spectrum_oids()
    fake = _FakeWritableSnmpClient(get_responses=[5, 4])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)

    # Settings default is 15 (explicit, NOT the field default of 30).
    # The override of 2 wins. Issue #70 / WU-2: ``final_status == 4``
    # triggers the post-sweep HTTP ladder; we minimise its retry /
    # timeout so the unreachable TEST-NET-1 host cannot blow the
    # test runtime.
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=0,
        nora_spectrum_sweep_duration_seconds=15,
        nora_spectrum_sweep_poll_interval_seconds=0.05,
        nora_spectrum_sweep_timeout_seconds=10,
        nora_spectrum_http_timeout_seconds=1.0,
        nora_spectrum_http_max_retries=0,
        nora_spectrum_http_retry_delay_seconds=0.1,
        nora_spectrum_sm_reassociation_timeout_seconds=1.0,
    )

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        sweep_duration_seconds=2,
    )

    assert result.sweep_duration_seconds == 2
    # First SET carries the override value, not the Settings default.
    assert fake.set_calls[0] == (spec_oids["spectrumScanDuration"], 2), (
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
        # PR #66 review follow-up #3: short sweep duration so the
        # scaled guard activates at t=2s, not t=30s with the new default.
        sweep_duration_seconds=2,
    )

    # Frozen model — assignment raises.
    with pytest.raises(Exception):  # noqa: BLE001 — Pydantic frozen raises ValidationError
        result.scan_outcome = "TIMEOUT"  # type: ignore[misc]

    # JSON-serialisable for the MCP tool boundary.
    dumped = result.model_dump(mode="json")
    assert dumped["scan_outcome"] == "COMPLETED"
    assert dumped["final_status"] == 0  # queue was [0]; helper accepts 0 at t≈2s
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
        # PR #66 review follow-up #3: explicit short sweep duration so
        # the guard `elapsed >= sweep_duration_seconds` activates at
        # t=2s (not t=30s with the new default).
        sweep_duration_seconds=2,
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
        sweep_duration_seconds=2,
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
        sweep_duration_seconds=2,
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


# ---------------------------------------------------------------------------
# PR #66 review follow-up #3 — scaled guard + TDD buffer flush scenario
#
# After commit `a607eb9` shipped the 1.0s startup guard and the
# DriverError swallow, the operator re-deployed to physical PMP 450i
# hardware and observed that the 1.0s guard is still insufficient:
# the radio's SNMP agent takes 1.5-2.5s to flush TDD buffers and
# engage sweep mode, so at t=1.087s (within the 1.0s guard) the
# radio was STILL returning its pre-sweep status 4. The helper
# reported false-positive completion in 1.087s while the sweep had
# not started.
#
# Fixes (in this follow-up):
#   (a) Removed `_MIN_STARTUP_GUARD_SECONDS` constant.
#   (b) `_poll_sweep_status` guard is now
#       `elapsed >= float(sweep_duration_seconds)` with no minimum
#       floor — "a timed sweep cannot physically complete before
#       the requested duration" (operator's recommendation).
#   (c) `nora_spectrum_sweep_duration_seconds` default 15 → 30
#       (operator's production baseline for "sufficient RF sampling
#       across unforeseen noise bursts").
#
# Tests in this section pin:
#   - `test_sweep_does_not_complete_before_hardware_tdd_flush` — the
#     operator's exact observed sequence: pre-state 4 for the first
#     1.0s (5 polls × 0.2s) → 5 (in-progress, TDD flush done) → 4
#     (idle-complete). With `sweep_duration_seconds=2` the scaled
#     guard fires at t=2.0s and prevents the false-positive
#     acceptance that the old 1.0s floor would have produced.
#   - `test_sweep_guard_waits_full_sweep_duration_before_completion`
#     — pin the scaled-guard semantics at `sweep_duration_seconds=2`:
#     completion sentinels are NOT accepted before t=2.0s regardless
#     of what the radio returns.
#   - `test_sweep_guard_scales_with_sweep_duration_seconds` — verify
#     linearity: the guard floor scales with `sweep_duration_seconds`
#     (3s sweep → 3s floor).
#   - `test_sweep_driver_error_mid_poll_is_swallowed` — updated to
#     use the new scaled guard (2s instead of the old 1.0s floor).
#
# ---------------------------------------------------------------------------

# The tests below pin both behaviours against the documented
# operator-observed scenarios.
# ---------------------------------------------------------------------------


def test_sweep_does_not_complete_before_hardware_tdd_flush(tmp_path: Path) -> None:
    """Simulates the operator's TDD buffer flush scenario (PR #66 follow-up #3).

    After re-deploying ``a607eb9`` to physical PMP 450i hardware
    (firmware 25.0.1), the operator observed that the radio's SNMP
    agent takes 1.5-2.5s to flush TDD buffers and engage sweep mode.
    At t=1.087s (within the 1.0s guard floor) the radio was STILL
    returning its pre-sweep status 4, and the helper reported
    false-positive completion while the sweep had not started.

    This test pins the scaled-guard replacement against the
    operator's exact observed sequence:

      - First 5 polls (every 0.2s → covers the first 1.0s) return
        pre-state 4 (the radio has not yet engaged sweep mode).
      - 6th poll returns 5 (in-progress — TDD buffer flush
        completed and the radio entered sweep mode).
      - 7th poll returns 4 (idle-complete — sweep finished).

    With ``sweep_duration_seconds=2`` and ``poll_interval_seconds=0.2``,
    the scaled guard `elapsed >= sweep_duration_seconds` activates at
    t=2.0s. The helper MUST poll past the pre-state 4 window (which
    would have falsely tripped the old 1.0s guard), observe the
    in-progress 5, and accept the completion 4 only after the scaled
    guard elapses. Runtime: ~2s.
    """
    import time

    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)

    # Operator-observed sequence: pre-state 4 for the first 1.0s (5
    # polls at 0.2s = 1.0s), then 5 (in-progress — TDD flush done),
    # then 4 (idle-complete).
    fake = _FakeWritableSnmpClient(get_responses=[4] * 5 + [5, 4])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=0,
        nora_spectrum_sweep_poll_interval_seconds=0.2,
        nora_spectrum_sweep_timeout_seconds=10,
        # Issue #70 / WU-2: ``final_status == 4`` triggers the
        # post-sweep HTTP ladder. The TEST-NET-1 host (192.0.2.10)
        # is unreachable from the test runner, so we minimise the
        # ladder's HTTP retry/timeout to keep this guard-semantics
        # test within its ``elapsed < 5.0`` upper bound. The ladder
        # is non-fatal — it just records the failure in
        # ``post_sweep_error`` and returns empty bin fields.
        nora_spectrum_http_timeout_seconds=1.0,
        nora_spectrum_http_max_retries=0,
        nora_spectrum_http_retry_delay_seconds=0.1,
    )

    start = time.monotonic()
    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        # PR #66 review follow-up #3: scaled guard `elapsed >= sweep_duration_seconds`.
        # The 1.0s startup floor (removed) would have falsely accepted
        # the pre-state 4 in this scenario; the new guard waits the
        # full 2.0s.
        sweep_duration_seconds=2,
    )
    elapsed = time.monotonic() - start

    # The helper polled past the scaled 2.0s guard before accepting
    # completion — the pre-state 4 on the first 5 polls did NOT
    # trigger an early return (which would have falsely tripped the
    # old 1.0s floor on physical hardware).
    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4
    assert elapsed >= 2.0, (
        f"helper returned too early at {elapsed:.2f}s; the scaled "
        f"guard `elapsed >= sweep_duration_seconds` was bypassed"
    )
    # Did not approach the 10s timeout (sweep) OR the post-sweep
    # ladder's 1.0s HTTP timeout (WU-2 — see Settings above).
    assert elapsed < 5.0, f"helper took {elapsed:.2f}s — should not approach the 10s timeout"
    # The helper polled well past the 1.0s pre-state 4 window (which
    # would have tripped the old 1.0s floor) AND past the scaled
    # 2.0s guard. With poll_interval=0.2s the guard activates at
    # poll index >= 10; the helper must have polled at least 10
    # times before accepting 4.
    assert len(fake.get_calls) >= 10, (
        f"expected >=10 GETs past the 2.0s scaled guard; got {len(fake.get_calls)} GETs"
    )
    # Client lifecycle still honoured.
    assert fake.closed is True


def test_sweep_guard_waits_full_sweep_duration_before_completion(tmp_path: Path) -> None:
    """Scaled guard `elapsed >= sweep_duration_seconds` rejects sentinels before duration.

    PR #66 review follow-up #3: the 1.0s startup floor was replaced
    with a sweep-duration-scaled guard. A timed sweep cannot
    physically complete before the requested duration, so completion
    sentinels MUST NOT be accepted until ``elapsed >=
    sweep_duration_seconds`` regardless of what the radio returns.
    With ``sweep_duration_seconds=2`` and ``poll_interval_seconds=0.05``,
    the guard activates at poll index >= 40 (2.0s / 0.05s).

    Queue design: in-progress (5) for 40 polls (the helper does NOT
    accept these before the scaled guard fires), then 4 (completion
    sentinel). The helper MUST poll past the guard before accepting
    4 — and it MUST NOT accept the 4 on a hypothetical early return
    at poll index 0. Runtime: ~2s.
    """
    import time

    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)

    # 40 in-progress polls (would-be queue length to reach the 2.0s
    # scaled guard floor at poll_interval=0.05), then 4 (completion).
    fake = _FakeWritableSnmpClient(get_responses=[5] * 40 + [4])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=0,
        nora_spectrum_sweep_poll_interval_seconds=0.05,
        nora_spectrum_sweep_timeout_seconds=10,
        # Issue #70 / WU-2: keep the post-sweep HTTP ladder from
        # blocking on the unreachable TEST-NET-1 host (192.0.2.10)
        # so the scaled-guard elapsed-time upper bound holds.
        nora_spectrum_http_timeout_seconds=1.0,
        nora_spectrum_http_max_retries=0,
        nora_spectrum_http_retry_delay_seconds=0.1,
    )

    start = time.monotonic()
    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        sweep_duration_seconds=2,
    )
    elapsed = time.monotonic() - start

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4
    # Helper polled past the 2.0s scaled guard before accepting.
    assert elapsed >= 2.0, (
        f"helper returned too early at {elapsed:.2f}s; the scaled "
        f"`elapsed >= sweep_duration_seconds` guard was bypassed"
    )
    # Did not approach the 10s sweep timeout OR the 1.0s post-sweep
    # HTTP ladder timeout (WU-2 — see Settings above).
    assert elapsed < 5.0, f"helper took {elapsed:.2f}s — should not approach the 10s timeout"
    # Helper polled enough times to reach the scaled guard.
    assert len(fake.get_calls) >= 40, (
        f"expected >=40 GETs past the 2.0s scaled guard; got {len(fake.get_calls)} GETs"
    )
    assert fake.closed is True


def test_sweep_guard_scales_with_sweep_duration_seconds(tmp_path: Path) -> None:
    """The scaled guard scales linearly with `sweep_duration_seconds`.

    PR #66 review follow-up #3: with ``sweep_duration_seconds=3`` and
    ``poll_interval_seconds=0.05``, the guard activates at poll
    index >= 60 (3.0s / 0.05s) — NOT at poll index >= 40 (which would
    be the 2.0s guard). A 3-second sweep must NOT be accepted at
    t≈2s; the helper must wait the full 3.0s.

    This pins the linearity property of the scaled guard: the
    guard floor is `sweep_duration_seconds`, and doubling the
    duration doubles the floor.
    """
    import time

    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)

    # 60 in-progress polls (queue length to reach the 3.0s guard
    # floor at poll_interval=0.05), then 4 (completion).
    fake = _FakeWritableSnmpClient(get_responses=[5] * 60 + [4])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=0,
        nora_spectrum_sweep_poll_interval_seconds=0.05,
        nora_spectrum_sweep_timeout_seconds=10,
        # Issue #70 / WU-2: see Settings note in
        # ``test_sweep_does_not_complete_before_hardware_tdd_flush``
        # — minimise the post-sweep HTTP ladder so the unreachable
        # TEST-NET-1 host cannot blow past this test's
        # ``elapsed < 6.0`` upper bound.
        nora_spectrum_http_timeout_seconds=1.0,
        nora_spectrum_http_max_retries=0,
        nora_spectrum_http_retry_delay_seconds=0.1,
    )

    start = time.monotonic()
    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        sweep_duration_seconds=3,
    )
    elapsed = time.monotonic() - start

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4
    # Helper waited at least 3.0s (the 3s guard floor) before
    # accepting — NOT 2.0s (which would be the 2s guard floor or the
    # old 1.0s startup floor).
    assert elapsed >= 3.0, (
        f"helper returned too early at {elapsed:.2f}s; the 3.0s "
        f"scaled guard was bypassed (would have been bypassed at 2.0s "
        f"with the 2s guard too)"
    )
    assert elapsed < 6.0, f"helper took {elapsed:.2f}s — should not approach the 10s timeout"
    # Helper polled enough times to reach the 3.0s guard floor.
    assert len(fake.get_calls) >= 60, (
        f"expected >=60 GETs past the 3.0s scaled guard; got {len(fake.get_calls)} GETs"
    )
    assert fake.closed is True


def test_sweep_driver_error_mid_poll_is_swallowed(tmp_path: Path) -> None:
    """Mid-sweep ``SnmpTimeoutError`` (``DriverError`` subclass) is swallowed.

    Operator observation on physical PMP 450i: during an active sweep
    the radio goes off-channel and ``client.get_oid`` raises
    ``SnmpTimeoutError`` / ``NetworkUnreachableError`` (subclasses of
    ``DriverError``). The previous ``except (KeyError, ValueError,
    TypeError)`` block did NOT catch these, so a single mid-sweep
    packet loss would abort the helper with an unhandled exception.

    The expanded ``except (KeyError, ValueError, TypeError, DriverError)``
    handler collapses transient wire failures to ``last_status = -1``
    and keeps polling. This test injects ``SnmpTimeoutError`` for the
    first 5 polls, then a normal completion sentinel for the 6th. The
    helper must NOT propagate the exception; it must eventually
    complete with ``final_status=4``.

    PR #66 review follow-up #3: the helper also waits for the scaled
    guard ``elapsed >= sweep_duration_seconds`` before accepting
    completion; ``sweep_duration_seconds=2`` keeps runtime at ~2s.
    """
    import time

    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)

    # First 5 polls raise ``SnmpTimeoutError`` (a ``DriverError``
    # subclass); then ``[4]`` queue serves completion. The scaled
    # guard ensures we poll past 2.0s before accepting the 4.
    fake = _FakeErrorInjectingSnmpClient(
        get_responses=[4],
        n_error_polls=5,
    )
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_maintenance_window_minutes=0,
        nora_spectrum_sweep_poll_interval_seconds=0.05,
        nora_spectrum_sweep_timeout_seconds=10,
        # Issue #70 / WU-2: see Settings note in
        # ``test_sweep_does_not_complete_before_hardware_tdd_flush`` —
        # keep the post-sweep HTTP ladder from blocking on the
        # unreachable TEST-NET-1 host.
        nora_spectrum_http_timeout_seconds=1.0,
        nora_spectrum_http_max_retries=0,
        nora_spectrum_http_retry_delay_seconds=0.1,
        nora_spectrum_sm_reassociation_timeout_seconds=1.0,
    )

    start = time.monotonic()
    # The expanded exception handler swallows the DriverError; the
    # helper does NOT propagate the SnmpTimeoutError to the caller.
    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        # PR #66 review follow-up #3: scaled guard waits 2.0s before
        # accepting completion (not the 1.0s floor of follow-up #2).
        sweep_duration_seconds=2,
    )
    elapsed = time.monotonic() - start

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4
    # The helper waited past the 2.0s scaled guard.
    assert elapsed >= 2.0, (
        f"helper returned too early at {elapsed:.2f}s; the 2.0s scaled guard was bypassed"
    )
    # The fake was polled enough times to exhaust the 5 error polls
    # AND wait past the scaled guard.
    assert len(fake.get_calls) >= 5, (
        f"expected >=5 GETs (5 error polls + completion poll); got {len(fake.get_calls)}"
    )
    assert fake.closed is True


# ---------------------------------------------------------------------------
# Issue #70 / WU-2 — post-sweep HTTP fetch + XML parse ladder
#
# These tests pin the WU-2 contract:
#   - ``final_status == 4`` triggers the HTTP ladder (AP XML + optional
#     SM hosts; parse + noise-floor + ranking).
#   - ``final_status`` in ``{0, 3}`` SKIPS the ladder entirely.
#   - Any HTTP / parse failure is NON-FATAL — the sweep outcome stays
#     COMPLETED, the bin fields go to empty, and ``post_sweep_error``
#     captures a one-line diagnostic.
# Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals.
# ---------------------------------------------------------------------------


def _raising_httpx_client(*_args: Any, **_kwargs: Any) -> httpx.Client:
    """Sentinel replacement for ``httpx.Client`` used to assert the ladder didn't run.

    Any call into ``fetch_spectrum_xml`` (which constructs
    ``httpx.Client(...)`` when no ``http_client`` is passed) raises
    ``RuntimeError`` so a leaked ladder trips the test loudly instead
    of silently hitting the network.
    """
    raise RuntimeError("post-sweep HTTP ladder must not run")


def test_post_sweep_ladder_populates_noise_and_ranking_on_sentinel_4(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``final_status == 4`` runs the ladder and populates bins + ranking.

    Issue #70 / WU-2: the helper writes ``ranked_clean_frequencies``
    and ``noise_floor_dbm`` from the parsed AP XML. The fixture's
    cleanest freq is 3560.0 MHz (worst-leg avg ``-81`` dBm across the
    V/H legs).
    """
    from nora.drivers.snmp_pmp450i.spectrum import (
        SpectrumSweepResult,
        fetch_spectrum,
    )

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[5, 4])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    fake_http = _build_fake_http_client(ap_xml=_load_fixture_xml())
    monkeypatch.setattr(_spectrum_http_module.httpx, "Client", lambda **kw: fake_http)

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        sweep_duration_seconds=2,
    )

    assert isinstance(result, SpectrumSweepResult)
    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4
    assert result.post_sweep_error == ""
    # Fixture has 8 unique frequencies × 4 series each; top-N=10 keeps
    # all 8.
    assert len(result.ranked_clean_frequencies) == 8
    assert len(result.noise_floor_dbm) == 8
    # Cleanest = 3560.0 MHz (worst-leg avg = -81 dBm); see fixture.
    assert result.ranked_clean_frequencies[0] == 3560.0


def test_post_sweep_ladder_skipped_on_sentinel_3_idle_no_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sentinel 3 (``idleNoSpectrumAnalysis``) SKIPS the ladder entirely.

    The sweep ran but produced no usable results; the ladder would
    only fetch garbage or 404. The helper returns empty bin fields
    with ``post_sweep_error == ""`` and never invokes ``httpx.Client``.
    """
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[5, 3])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    monkeypatch.setattr(_spectrum_http_module.httpx, "Client", _raising_httpx_client)

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        sweep_duration_seconds=2,
    )

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 3
    assert result.ranked_clean_frequencies == []
    assert result.noise_floor_dbm == {}
    assert result.post_sweep_error == ""


def test_post_sweep_ladder_skipped_on_sentinel_0_defensive_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sentinel 0 (defensive) SKIPS the ladder too — no fetch, no error."""
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[5, 0])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    monkeypatch.setattr(_spectrum_http_module.httpx, "Client", _raising_httpx_client)

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        sweep_duration_seconds=2,
    )

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 0
    assert result.ranked_clean_frequencies == []
    assert result.noise_floor_dbm == {}
    assert result.post_sweep_error == ""


def test_post_sweep_ladder_http_failure_is_non_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transport failure on the AP fetch is NON-FATAL.

    The helper catches :class:`SpectrumHttpFetchError` /
    :class:`SpectrumXmlParseError` and folds them into
    ``post_sweep_error``. The sweep outcome stays COMPLETED so the
    orchestrator can distinguish "sweep ran fine, bin decode
    failed" from :class:`SpectrumSweepTimeout` (sweep itself timed
    out).
    """
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[5, 4])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    fake_http = _build_fake_http_client(ap_error=httpx.ConnectError("refused"))
    monkeypatch.setattr(_spectrum_http_module.httpx, "Client", lambda **kw: fake_http)

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        sweep_duration_seconds=2,
    )

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4
    assert result.ranked_clean_frequencies == []
    assert result.noise_floor_dbm == {}
    assert result.post_sweep_error != ""
    assert "192.0.2.10" in result.post_sweep_error


def test_post_sweep_ladder_xml_parse_failure_is_non_fatal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An XML payload that fails parsing is NON-FATAL.

    The fake HTTP client returns 200 with body ``"<Other/>"``, which
    the :func:`parse_spectrum_xml` helper rejects with
    :class:`SpectrumXmlParseError` (root element is not
    ``<Spectrum_Analyzer>``). The helper folds the error into
    ``post_sweep_error`` and returns empty bin fields.
    """
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[5, 4])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    fake_http = _build_fake_http_client(ap_xml="<Other/>")
    monkeypatch.setattr(_spectrum_http_module.httpx, "Client", lambda **kw: fake_http)

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        sweep_duration_seconds=2,
    )

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4
    assert result.post_sweep_error != ""
    assert result.ranked_clean_frequencies == []
    assert result.noise_floor_dbm == {}


def test_post_sweep_ladder_with_sm_hosts_fetches_sequentially(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two SM hosts are fetched in order; bins aggregate; ranking picks cleanest.

    Both SM hosts return the same fixture XML as the AP, so the
    aggregated bins produce 8 unique frequency keys (the noise-floor
    helper picks the worst-leg per frequency, not the union).
    """
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[5, 4])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    # NB: ``nora_spectrum_sm_reassociation_timeout_seconds=1.0`` is
    # the minimum the validator allows (the spec's default is 15s);
    # ``time.sleep`` is monkey-patched to a no-op below so the helper
    # still spends zero wall time on the wait.
    settings = _settings_no_window()

    fixture_xml = _load_fixture_xml()
    fake_http = _build_fake_http_client(
        ap_xml=fixture_xml,
        sm_xml={
            "192.0.2.21": fixture_xml,
            "192.0.2.22": fixture_xml,
        },
    )
    monkeypatch.setattr(_spectrum_http_module.httpx, "Client", lambda **kw: fake_http)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        sweep_duration_seconds=2,
        sm_hosts=["192.0.2.21", "192.0.2.22"],
    )

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4
    assert result.post_sweep_error == ""
    # Noise floor collapses across the 3 hosts to 8 unique freq keys.
    assert len(result.noise_floor_dbm) == 8
    # Cleanest freq (worst-leg avg) survives the aggregation.
    assert result.ranked_clean_frequencies[0] == 3560.0


def test_post_sweep_ladder_sm_host_failure_still_uses_ap_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An SM-host failure is recorded but does NOT discard the AP bins."""
    from nora.drivers.snmp_pmp450i.spectrum import fetch_spectrum

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_spectrum_oids=True)
    fake = _FakeWritableSnmpClient(get_responses=[5, 4])
    driver = _build_driver(inventory=inv, registry=registry, fake_client=fake)
    settings = _settings_no_window()

    # SM host ``192.0.2.21`` is NOT in ``sm_xml``, so the fake handler
    # raises :class:`SpectrumHttpFetchError` (status=404) for that URL
    # — the helper's non-fatal catch folds it into ``post_sweep_error``.
    fake_http = _build_fake_http_client(ap_xml=_load_fixture_xml(), sm_xml={})
    monkeypatch.setattr(_spectrum_http_module.httpx, "Client", lambda **kw: fake_http)
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    result = fetch_spectrum(
        driver=driver,
        device_id="ap-7400-01",
        settings=settings,
        operator_confirmed=True,
        sweep_duration_seconds=2,
        sm_hosts=["192.0.2.21"],
    )

    assert result.scan_outcome == "COMPLETED"
    assert result.final_status == 4
    # AP bins drive the ranking — the SM failure is recorded but
    # does NOT discard AP data.
    assert len(result.ranked_clean_frequencies) == 8
    assert len(result.noise_floor_dbm) == 8
    assert result.ranked_clean_frequencies[0] == 3560.0
    # Diagnostic surfaces the offending SM host.
    assert result.post_sweep_error != ""
    assert "192.0.2.21" in result.post_sweep_error


def test_post_sweep_error_default_is_empty_string() -> None:
    """``SpectrumSweepResult.post_sweep_error`` defaults to ``""``."""
    from nora.drivers.snmp_pmp450i.spectrum import SpectrumSweepResult

    result = SpectrumSweepResult(
        device_id="192.0.2.10",
        scan_started_at="2026-09-19T00:00:00+00:00",
        scan_completed_at="2026-09-19T00:00:30+00:00",
        sweep_duration_seconds=30,
        final_status=4,
        scan_outcome="COMPLETED",
    )
    assert result.post_sweep_error == ""


__all__ = [
    "test_post_sweep_ladder_populates_noise_and_ranking_on_sentinel_4",
    "test_post_sweep_ladder_skipped_on_sentinel_3_idle_no_results",
    "test_post_sweep_ladder_skipped_on_sentinel_0_defensive_abort",
    "test_post_sweep_ladder_http_failure_is_non_fatal",
    "test_post_sweep_ladder_xml_parse_failure_is_non_fatal",
    "test_post_sweep_ladder_with_sm_hosts_fetches_sequentially",
    "test_post_sweep_ladder_sm_host_failure_still_uses_ap_data",
    "test_post_sweep_error_default_is_empty_string",
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
    # PR #66 review follow-up #1 — expanded sentinel set
    "test_sweep_completes_with_status_3_idle_no_results",
    "test_sweep_completes_with_status_4_idle_complete",
    "test_sweep_completes_with_status_0_post_abort",
    "test_sweep_timeout_when_poll_sees_status_5_in_progress",
    "test_sweep_timeout_when_poll_sees_sentinel_minus_1",
    "test_sweep_completion_constants_exported",
    # PR #66 review follow-up #3 — TDD buffer flush + scaled guard
    "test_sweep_does_not_complete_before_hardware_tdd_flush",
    "test_sweep_guard_waits_full_sweep_duration_before_completion",
    "test_sweep_guard_scales_with_sweep_duration_seconds",
    "test_sweep_driver_error_mid_poll_is_swallowed",
]
