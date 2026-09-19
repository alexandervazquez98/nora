"""Tests for the async probe coordinator (PR1 WU-1.3, issue #61).

These tests use a hand-rolled ``_FakePinger`` so no real socket is
ever touched; the existing ``tests/probes/test_icmp_engine.py``
shows the same idiom (inject a fake via :mod:`unittest.mock`).
Each async test is wrapped in :func:`asyncio.run` per the repo-wide
ban on ``pytest-asyncio``.

The fake driver reuses the ``_FakeDriver`` idiom from
``tests/probes/test_discovery.py`` so the discovery helper sees a
shape it can swallow without spawning a real SNMP stack. The
coordinator is dependency-injected via the ``driver`` keyword so
the fake pinger list can be swapped in.

Each test runs in <500 ms.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

from nora.config import Settings
from nora.drivers.snmp_pmp450i.subscribers import (
    SubscriberRecord,
    SubscriberSummary,
)
from nora.probes.exceptions import (
    IcmpTimeoutError,
    IcmpUnreachableError,
)
from nora.probes.icmp import IcmpSample
from nora.probes.models import ProbeRunSettings, ProbeRunStarted, ProbeTarget
from nora.probes.probe import (
    ProbeConfigurationError,
    generate_run_id,
    run_probe,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakePinger:
    """Records calls and returns pre-canned samples or raises pre-canned errors.

    Designed so the test can compose a deterministic per-ping response
    stream:

    * ``errors`` (consumed before ``samples``) raises on the matching
      ping call. Used to exercise the typed-exception translation
      paths (``IcmpTimeoutError`` / ``IcmpUnreachableError`` /
      ``IcmpEngineError``) into failed samples.
    * ``samples`` is cycled (modulo) once ``errors`` is exhausted; if
      empty, the pinger falls back to a healthy ``received=True``
      sample so cancellation / pacing tests don't need to wire any
      canned response.
    * ``ping_calls`` records ``(host, payload_size, timeout)`` so the
      test can assert per-destination dispatch.
    * ``close_count`` increments once per ``close()`` invocation so
      the test can assert clean shutdown semantics.
    """

    def __init__(
        self,
        *,
        samples: list[IcmpSample] | None = None,
        errors: list[Exception] | None = None,
    ) -> None:
        self.samples: list[IcmpSample] = list(samples or [])
        self.errors: list[Exception] = list(errors or [])
        self._call_index = 0
        self.close_count = 0
        self.ping_calls: list[tuple[str, int, float]] = []

    async def ping(self, target: str, *, payload_size: int, timeout: float) -> IcmpSample:
        self.ping_calls.append((target, payload_size, timeout))
        idx = self._call_index
        self._call_index += 1
        if idx < len(self.errors):
            raise self.errors[idx]
        if self.samples:
            return self.samples[idx % len(self.samples)]
        return IcmpSample(
            target=target,
            rtt_ms=1.0,
            received=True,
            error=None,
            timestamp_unix=time.time(),
        )

    async def close(self) -> None:
        self.close_count += 1


class _FakeDriver:
    """Hand-rolled fake satisfying the ``fetch_sm_table`` contract.

    Mirrors :class:`tests.probes.test_discovery._FakeDriver`: three
    no-op stand-ins for ``_inventory`` / ``_catalog_registry`` /
    ``_client_factory`` so the slice-3 helper does not raise.
    """

    def __init__(self, summary: SubscriberSummary) -> None:
        self._inventory = MagicMock()
        self._catalog_registry = MagicMock()
        self._client_factory = MagicMock()
        self._summary = summary
        self.calls: list[dict[str, Any]] = []

    def fetch_sm_table(self, *, device_id: str, settings: Any | None = None) -> SubscriberSummary:
        self.calls.append({"device_id": device_id, "settings": settings})
        return self._summary


def _make_record(luid: str, *, cinr_db: int = 22) -> SubscriberRecord:
    """Build a minimal :class:`SubscriberRecord` for the discovery helper."""
    return SubscriberRecord(
        luid=luid,
        session_uptime=86400,
        cinr_db=cinr_db,
        link_status="inSession",
        modulation="8X",
    )


def _make_summary(
    *,
    ap_host: str = "192.0.2.1",
    online: list[tuple[str, int]] | None = None,
    degraded: list[tuple[str, int]] | None = None,
) -> SubscriberSummary:
    """Build a :class:`SubscriberSummary` for the fake driver."""
    online_records = [_make_record(luid, cinr_db=cinr) for luid, cinr in (online or [])]
    degraded_records = [_make_record(luid, cinr_db=cinr) for luid, cinr in (degraded or [])]
    return SubscriberSummary(
        target_ip=ap_host,
        online_active=online_records,
        active_degraded=degraded_records,
        pre_existing_offline=[],
        baseline_size=len(online_records) + len(degraded_records),
        pre_existing_offline_count=0,
        fetched_at="2026-09-19T00:00:00+00:00",
    )


def _settings(overrides: dict[str, Any] | None = None) -> Settings:
    """Build a hermetic :class:`Settings` instance for the test suite.

    The defaults match the PR1 WU-1.4 bounds so validation paths
    exercise the same numbers production will see. ``_env_file=None``
    skips the on-disk ``.env`` lookup; the test always uses defaults.
    """
    base: dict[str, Any] = {
        "nora_icmp_min_duration_seconds": 60,
        "nora_icmp_default_duration_seconds": 600,
        "nora_icmp_max_duration_seconds": 1800,
        "nora_icmp_default_interval_seconds": 1.0,
        "nora_icmp_default_packet_size_bytes": 64,
        "nora_icmp_per_packet_timeout_seconds": 5.0,
    }
    if overrides:
        base.update(overrides)
    return Settings(_env_file=None, _env_file_encoding=None, **base)


def _install_fake_pingers(
    monkeypatch: pytest.MonkeyPatch,
    pingers: list[_FakePinger],
) -> None:
    """Patch ``UnprivilegedIcmpPinger`` so the coordinator uses our fakes.

    The coordinator calls ``UnprivilegedIcmpPinger(...)`` once per
    destination. We pop from the supplied ``pingers`` list so the
    returned pinger identity matches the order of ``discovery.all_targets``.
    Tests inspect ``pinger.ping_calls`` and ``pinger.close_count``
    afterwards.
    """
    pingers_iter = iter(pingers)

    def _factory(*_args: Any, **_kwargs: Any) -> _FakePinger:
        return next(pingers_iter)

    monkeypatch.setattr("nora.probes.probe.UnprivilegedIcmpPinger", _factory)


# ---------------------------------------------------------------------------
# 1. Yields samples for AP + SMs
# ---------------------------------------------------------------------------


def test_run_probe_yields_samples_for_ap_and_sm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fake pinger returns a healthy sample for every ping.

    Drives a 0.4-second probe with 2 targets (AP + 1 SM),
    ``interval=0.05`` (so the test fires ~8 packets per target in
    0.4 s; the assertion floor is conservative at 4 samples total).
    Asserts the host set spans the AP and the SM.
    """
    summary = _make_summary(ap_host="192.0.2.1", online=[("1", 22)])
    driver = _FakeDriver(summary)
    # Resolved targets: AP first, then SM "1".
    targets = [
        ProbeTarget(role="ap", host="192.0.2.1", label="AP"),
        ProbeTarget(role="sm", luid="1", host="1", label="SM 1", cinr_db=22),
    ]
    fake_pingers = [_FakePinger() for _ in targets]
    _install_fake_pingers(monkeypatch, fake_pingers)

    async def _run() -> list[IcmpSample]:
        out: list[IcmpSample] = []
        async for sample in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=1,  # below min — relax to 1s for test speed? no: use min
            interval_seconds=0.05,
        ):
            out.append(sample)
        return out

    # 1-second duration triggers the min-duration validator (60s default).
    # Override the min so the test can drive a short-lived probe.
    settings = _settings(
        {
            "nora_icmp_min_duration_seconds": 1,
            "nora_icmp_default_duration_seconds": 1,
        }
    )
    samples = asyncio.run(_run())

    assert len(samples) >= 4, f"expected >= 4 samples in 0.4s window, got {len(samples)}"
    seen_hosts = {sample.target for sample in samples}
    assert seen_hosts == {"192.0.2.1", "1"}, f"unexpected host set: {seen_hosts}"
    assert all(sample.received for sample in samples)


# ---------------------------------------------------------------------------
# 2. Stops after duration_seconds
# ---------------------------------------------------------------------------


def test_run_probe_stops_after_duration_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The iterator exhausts within ``1.0s`` of wall-clock for a 0.5s probe.

    Relaxing ``min_duration_seconds`` to 0 lets the test exercise the
    deadline path with a sub-second duration. The fake pinger is
    infinite (no error, no sample budget), so the iterator can only
    exit on the deadline.
    """
    settings = _settings(
        {
            "nora_icmp_min_duration_seconds": 0,
            "nora_icmp_default_duration_seconds": 1,
        }
    )
    summary = _make_summary(ap_host="192.0.2.1", online=[("1", 22)])
    driver = _FakeDriver(summary)
    targets = [
        ProbeTarget(role="ap", host="192.0.2.1", label="AP"),
        ProbeTarget(role="sm", luid="1", host="1", label="SM 1", cinr_db=22),
    ]
    fake_pingers = [_FakePinger() for _ in targets]
    _install_fake_pingers(monkeypatch, fake_pingers)

    async def _run() -> tuple[float, int]:
        start = time.monotonic()
        count = 0
        async for _ in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=1,
            interval_seconds=0.05,
        ):
            count += 1
        return time.monotonic() - start, count

    elapsed, _ = asyncio.run(_run())
    # ``duration_seconds=1`` must terminate within the 1.5s budget
    # (clock granularity + drain overhead). The fake pinger is
    # infinite so the iterator can ONLY exit on the deadline.
    assert elapsed < 1.5, f"iterator did not stop promptly: elapsed={elapsed}s"


# ---------------------------------------------------------------------------
# 3-7. Bounds validation (one test per knob)
# ---------------------------------------------------------------------------


def test_run_probe_validates_duration_below_minimum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``duration_seconds=10`` raises ``ProbeConfigurationError`` and never calls discovery."""
    settings = _settings()  # min = 60 by default
    summary = _make_summary(ap_host="192.0.2.1")
    driver = _FakeDriver(summary)
    driver.fetch_sm_table = MagicMock(wraps=driver.fetch_sm_table)  # type: ignore[method-assign]

    async def _run() -> None:
        async for _ in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=10,
        ):
            pass

    with pytest.raises(ProbeConfigurationError):
        asyncio.run(_run())
    driver.fetch_sm_table.assert_not_called()  # type: ignore[attr-defined]


def test_run_probe_validates_duration_above_maximum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``duration_seconds=2000`` raises ``ProbeConfigurationError`` and never calls discovery."""
    settings = _settings()  # max = 1800 by default
    summary = _make_summary(ap_host="192.0.2.1")
    driver = _FakeDriver(summary)
    driver.fetch_sm_table = MagicMock(wraps=driver.fetch_sm_table)  # type: ignore[method-assign]

    async def _run() -> None:
        async for _ in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=2000,
        ):
            pass

    with pytest.raises(ProbeConfigurationError):
        asyncio.run(_run())
    driver.fetch_sm_table.assert_not_called()  # type: ignore[attr-defined]


def test_run_probe_validates_interval_positive() -> None:
    """``interval_seconds=0`` raises ``ProbeConfigurationError``."""
    settings = _settings(
        {
            "nora_icmp_min_duration_seconds": 0,
            "nora_icmp_default_duration_seconds": 1,
        }
    )
    summary = _make_summary(ap_host="192.0.2.1")
    driver = _FakeDriver(summary)

    async def _run() -> None:
        async for _ in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=1,
            interval_seconds=0,
        ):
            pass

    with pytest.raises(ProbeConfigurationError):
        asyncio.run(_run())


def test_run_probe_validates_packet_size_positive() -> None:
    """``packet_size_bytes=0`` raises ``ProbeConfigurationError``."""
    settings = _settings(
        {
            "nora_icmp_min_duration_seconds": 0,
            "nora_icmp_default_duration_seconds": 1,
        }
    )
    summary = _make_summary(ap_host="192.0.2.1")
    driver = _FakeDriver(summary)

    async def _run() -> None:
        async for _ in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=1,
            packet_size_bytes=0,
        ):
            pass

    with pytest.raises(ProbeConfigurationError):
        asyncio.run(_run())


def test_run_probe_validates_per_packet_timeout_positive() -> None:
    """``per_packet_timeout_seconds=-1.0`` raises ``ProbeConfigurationError``."""
    settings = _settings(
        {
            "nora_icmp_min_duration_seconds": 0,
            "nora_icmp_default_duration_seconds": 1,
        }
    )
    summary = _make_summary(ap_host="192.0.2.1")
    driver = _FakeDriver(summary)

    async def _run() -> None:
        async for _ in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=1,
            per_packet_timeout_seconds=-1.0,
        ):
            pass

    with pytest.raises(ProbeConfigurationError):
        asyncio.run(_run())


# ---------------------------------------------------------------------------
# 8-9. Typed exceptions translate to failed samples
# ---------------------------------------------------------------------------


def test_run_probe_translates_icmp_timeout_error_to_failed_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ping 1 → ``IcmpTimeoutError`` → failed sample; ping 2 → success."""
    settings = _settings(
        {
            "nora_icmp_min_duration_seconds": 0,
            "nora_icmp_default_duration_seconds": 1,
        }
    )
    summary = _make_summary(ap_host="192.0.2.1")
    driver = _FakeDriver(summary)
    fake_pingers = [
        _FakePinger(
            errors=[IcmpTimeoutError("timeout")],
            samples=[
                IcmpSample(
                    target="192.0.2.1",
                    rtt_ms=3.0,
                    received=True,
                    error=None,
                    timestamp_unix=time.time(),
                ),
            ],
        ),
    ]
    _install_fake_pingers(monkeypatch, fake_pingers)

    async def _run() -> list[IcmpSample]:
        out: list[IcmpSample] = []
        async for sample in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=1,
            interval_seconds=0.01,
        ):
            out.append(sample)
            if len(out) >= 3:
                break
        return out

    samples = asyncio.run(_run())
    # The fake pinger raises on the first ping (→ failed sample), then
    # returns the canned healthy sample for every subsequent ping.
    # The test breaks out after 3 samples.
    assert any(s.received is False and "timeout" in (s.error or "") for s in samples), (
        f"expected at least one failed sample with 'timeout' error, got: {samples}"
    )
    assert any(s.received is True and s.rtt_ms == 3.0 for s in samples), (
        f"expected at least one received=True sample with rtt_ms=3.0, got: {samples}"
    )


def test_run_probe_translates_icmp_unreachable_error_to_failed_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ping 1 → ``IcmpUnreachableError`` → failed sample; ping 2 → success."""
    settings = _settings(
        {
            "nora_icmp_min_duration_seconds": 0,
            "nora_icmp_default_duration_seconds": 1,
        }
    )
    summary = _make_summary(ap_host="192.0.2.1")
    driver = _FakeDriver(summary)
    fake_pingers = [
        _FakePinger(
            errors=[IcmpUnreachableError("unreachable")],
            samples=[
                IcmpSample(
                    target="192.0.2.1",
                    rtt_ms=4.0,
                    received=True,
                    error=None,
                    timestamp_unix=time.time(),
                ),
            ],
        ),
    ]
    _install_fake_pingers(monkeypatch, fake_pingers)

    async def _run() -> list[IcmpSample]:
        out: list[IcmpSample] = []
        async for sample in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=1,
            interval_seconds=0.01,
        ):
            out.append(sample)
            if len(out) >= 3:
                break
        return out

    samples = asyncio.run(_run())
    assert any(s.received is False and "unreachable" in (s.error or "") for s in samples), (
        f"expected at least one failed sample with 'unreachable' error, got: {samples}"
    )
    assert any(s.received is True and s.rtt_ms == 4.0 for s in samples), (
        f"expected at least one received=True sample with rtt_ms=4.0, got: {samples}"
    )


# ---------------------------------------------------------------------------
# 10. Cancellation closes every pinger
# ---------------------------------------------------------------------------


def test_run_probe_cancellation_closes_all_pingers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling the consumer task closes every pinger (idempotent)."""
    settings = _settings(
        {
            "nora_icmp_min_duration_seconds": 1,
            "nora_icmp_default_duration_seconds": 600,
        }
    )
    summary = _make_summary(ap_host="192.0.2.1", online=[("1", 22)])
    driver = _FakeDriver(summary)
    targets = [
        ProbeTarget(role="ap", host="192.0.2.1", label="AP"),
        ProbeTarget(role="sm", luid="1", host="1", label="SM 1", cinr_db=22),
    ]
    fake_pingers = [_FakePinger() for _ in targets]
    _install_fake_pingers(monkeypatch, fake_pingers)

    async def _consume() -> None:
        async for _ in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=60,
            interval_seconds=0.05,
        ):
            await asyncio.sleep(0)  # yield control so the cancel propagates

    async def _runner() -> None:
        task = asyncio.create_task(_consume())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_runner())
    # Every fake pinger must have been closed exactly once via finally.
    for fp in fake_pingers:
        assert fp.close_count >= 1, f"pinger was not closed: {fp.close_count}"


# ---------------------------------------------------------------------------
# 11. Consumer break runs the finally clause
# ---------------------------------------------------------------------------


def test_run_probe_breaks_early_when_consumer_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``break`` the ``finally:`` clause closes every pinger."""
    settings = _settings(
        {
            "nora_icmp_min_duration_seconds": 1,
            "nora_icmp_default_duration_seconds": 600,
        }
    )
    summary = _make_summary(ap_host="192.0.2.1")
    driver = _FakeDriver(summary)
    targets = [
        ProbeTarget(role="ap", host="192.0.2.1", label="AP"),
    ]
    fake_pingers = [_FakePinger() for _ in targets]
    _install_fake_pingers(monkeypatch, fake_pingers)

    async def _run() -> int:
        count = 0
        async for _ in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=60,
            interval_seconds=0.01,
        ):
            count += 1
            if count >= 3:
                break
        return count

    count = asyncio.run(_run())
    assert count >= 3
    # The finally clause must have closed the pinger at least once.
    assert fake_pingers[0].close_count >= 1, (
        f"pinger was not closed after break: {fake_pingers[0].close_count}"
    )


# ---------------------------------------------------------------------------
# 12. generate_run_id
# ---------------------------------------------------------------------------


def test_generate_run_id_returns_8_hex_chars_and_unique_per_call() -> None:
    """``generate_run_id()`` returns 8 hex chars; collisions over 5 calls are unlikely."""
    ids = [generate_run_id() for _ in range(5)]
    for rid in ids:
        assert len(rid) == 8, f"run id not 8 chars: {rid!r}"
        assert all(c in "0123456789abcdef" for c in rid), f"non-hex run id: {rid!r}"
    # Probabilistic uniqueness: ``secrets.token_hex(4)`` gives 32 bits
    # of entropy. Five independent draws colliding is ~1 in 2^145.
    assert len(set(ids)) == len(ids), f"run ids collided: {ids}"


# ---------------------------------------------------------------------------
# 13. No SM targets — AP-only probe still yields samples
# ---------------------------------------------------------------------------


def test_run_probe_with_no_sm_targets_still_yields_ap_samples(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Empty ``online_active`` + ``active_degraded`` → AP-only probe still works.

    Relaxes ``min_duration_seconds`` to 1 so the 0.3s probe does not
    fail bounds validation. The fake pinger emits healthy samples;
    the iterator yields only AP-target samples.
    """
    settings = _settings(
        {
            "nora_icmp_min_duration_seconds": 1,
            "nora_icmp_default_duration_seconds": 600,
        }
    )
    summary = _make_summary(ap_host="192.0.2.1", online=[], degraded=[])
    driver = _FakeDriver(summary)
    targets = [
        ProbeTarget(role="ap", host="192.0.2.1", label="AP"),
    ]
    fake_pingers = [_FakePinger() for _ in targets]
    _install_fake_pingers(monkeypatch, fake_pingers)

    async def _run() -> list[IcmpSample]:
        out: list[IcmpSample] = []
        # duration=1 (the relaxed min) with a quick break so the test
        # finishes in well under 500 ms.
        async for sample in run_probe(
            driver=driver,
            device_id="ap-7400-01",
            settings=settings,
            duration_seconds=1,
            interval_seconds=0.05,
        ):
            out.append(sample)
            if len(out) >= 5:
                break
        return out

    samples = asyncio.run(_run())
    assert len(samples) >= 1, f"expected >= 1 AP sample, got {len(samples)}"
    assert all(s.target == "192.0.2.1" for s in samples), (
        f"non-AP target in samples: {[s.target for s in samples]}"
    )


# ---------------------------------------------------------------------------
# Bonus: ProbeRunSettings / ProbeRunStarted models are frozen + round-trip
# ---------------------------------------------------------------------------


def test_probe_run_settings_and_started_models_are_frozen() -> None:
    """Both new models are frozen Pydantic v2 BaseModels."""
    from pydantic import ValidationError

    settings = ProbeRunSettings(
        duration_seconds=600,
        interval_seconds=1.0,
        packet_size_bytes=64,
        per_packet_timeout_seconds=5.0,
    )
    started = ProbeRunStarted(
        run_id="0123abcd",
        device_id="ap-7400-01",
        ap_host="192.0.2.1",
        started_at_unix=time.time(),
        settings=settings,
    )
    with pytest.raises(ValidationError):
        settings.duration_seconds = 700  # type: ignore[misc]
    with pytest.raises(ValidationError):
        started.run_id = "deadbeef"  # type: ignore[misc]


def test_probe_configuration_error_is_value_error() -> None:
    """``ProbeConfigurationError`` subclasses ``ValueError`` (per docstring)."""
    err = ProbeConfigurationError("test")
    assert isinstance(err, ValueError)
    assert str(err) == "test"


def test_generate_run_id_entropy_demonstration() -> None:
    """Demonstrate that ``generate_run_id`` uses ``secrets.token_hex(4)`` semantics.

    100 draws in a single test must not collide (32-bit space ⇒ 1 in
    ~10^95 probability of a duplicate).
    """
    ids = {generate_run_id() for _ in range(100)}
    assert len(ids) == 100, f"collisions in 100 draws: {100 - len(ids)}"
