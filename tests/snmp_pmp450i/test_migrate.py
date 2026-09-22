"""Tests for WU-4 (issue #62): per-SM community overrides on the migration path.

These tests pin the WU-4 public contract for
``snmp_migrate_radio_frequency``:

* The optional ``sm_communities`` parameter (a ``dict[str, str]`` keyed
  by IP OR LUID) threads through ``fetch_migrate`` →
  ``_validate_sm_communities`` → ``_resolve_sm_community`` without
  regressing the legacy ``sm_communities=None`` / ``sm_communities={}``
  paths.
* Resolution order is **IP first, then LUID, then inventory**
  (operator decision 2026-09-19). The two ``OVERRIDE_*`` values win
  over the inventory fallback; if neither key matches, the SM keeps
  using its inventory community (no error, no warning).
* ``SmPreFlightResult.community_source`` records which credential won
  per SM; ``MigrationResult.sm_community_overrides_used`` aggregates
  the count of overrides that actually matched.
* The community string itself NEVER travels into the audit record —
  only the source label.
* Invalid override keys (e.g. an IP that the inventory does not
  carry, or a typo'd LUID) fall through to the inventory community
  without raising.

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real
IPs, hostnames, serials, or credentials.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import SecretStr

from nora.config import Settings
from nora.drivers.inventory import Device, Inventory
from nora.drivers.oid_catalog import OidCatalog, OidCatalogRegistry

# ---------------------------------------------------------------------------
# Helpers — inventory + catalog + fake client.
# ---------------------------------------------------------------------------


def _build_inventory_with_sms(
    tmp_path: Path,
    *,
    ap_community: str = "change-me-v2c",
    sm_luids: tuple[str, ...] = (),
    sm_community: str = "change-me-v2c",
) -> Inventory:
    """Hermetic inventory with one AP plus N SMs (one per LUID)."""
    devices: list[dict[str, Any]] = [
        {
            "device_id": "ap-7400-01",
            "vendor": "cambium",
            "model": "pmp450i",
            "firmware": "15.2.1",
            "host": "192.0.2.10",
            "snmp_version": "v2c",
            "community": ap_community,
        },
    ]
    for idx, luid in enumerate(sm_luids, start=20):
        devices.append(
            {
                "device_id": f"sm-7400-{luid}",
                "vendor": "cambium",
                "model": "pmp450i",
                "firmware": "15.2.1",
                "host": f"192.0.2.{idx}",
                "snmp_version": "v2c",
                "community": sm_community,
            }
        )
    payload = {"devices": devices}
    inv_path = tmp_path / "devices.yaml"
    inv_path.write_text(yaml.safe_dump(payload))
    return Inventory.from_yaml(inv_path)


def _build_catalog(firmware: str = "15.2.1") -> OidCatalogRegistry:
    """Minimal catalog registry covering the radio / SM / migration OIDs.

    Issue #80 (2026-09-22): the ``migrateCarrierFrequency`` /
    ``migratePriorCarrierFrequency`` OIDs now mirror the production
    catalogs under ``data/oid-catalogs/cambium/pmp450i/*.source.json``
    (``whispApsRFConfigRadioEntry.radioFreqCarrier``, the
    ``radioIndex=1`` instance at
    ``1.3.6.1.4.1.161.19.3.1.10.1.1.1.1``). The legacy placeholder
    (``1.3.6.1.4.1.161.19.3.1.4.1.38.0``) was actually
    ``radioUplinkRate`` — a read-only link OID, not a migration
    target — and was removed so the wire-path regression tests assert
    against the OID the helper will actually emit on the production
    wire. No other entry in the catalog changes.
    """
    oids: dict[str, str] = {
        "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.4.1.36.0",
        "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.4.1.38.0",
        "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.4.1.34.0",
        "eirp": "1.3.6.1.4.1.161.19.3.1.4.1.306.0",
        "ssr": "1.3.6.1.4.1.161.19.3.1.4.1.86.0",
        "modulationMode": "1.3.6.1.4.1.161.19.3.1.4.1.40.0",
        "smSessionUptime": "1.3.6.1.4.1.161.19.3.1.4.1.46.0",
        "smCinr": "1.3.6.1.4.1.161.19.3.1.4.1.74.0",
        "smLinkStatus": "1.3.6.1.4.1.161.19.3.1.4.1.19.0",
        "smLuid": "1.3.6.1.4.1.161.19.3.1.4.1.1.0",
        "migrateCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.10.1.1.1.1",
        "migratePriorCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.10.1.1.1.1",
        "rebootIfRequired": "1.3.6.1.4.1.161.19.3.3.3.4.0",
    }
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


class _RecordingFactory:
    """Records every ``Device`` passed to the per-SM client factory.

    The WU-4 carve-out threads a synthetic ``Device.model_copy`` into
    the factory when an override applies, so we capture the device's
    ``community`` per call to assert which credential actually reached
    the wire. We also count calls per host to sanity-check the
    per-SM probe loop.

    Issue #80 (2026-09-22): the factory now returns a
    ``WritableSnmpClient``-shaped stub (with ``set()`` instead of a
    synthetic ``apply_oid``) so the migration path can actually
    exercise the wire code in tests. The pre-flight path (sysDescr
    GET) only uses ``get_oid`` so the additional ``set`` verb is a
    no-op for that path. Each factory call mints a fresh client
    instance and tracks it so the per-client ``set`` calls can be
    inspected after ``fetch_migrate`` returns.
    """

    def __init__(
        self,
        *,
        sysdescr_per_host: dict[str, str] | None = None,
        prior_carrier_khz: int | None = None,
        reboot_if_required_vote: int | None = None,
    ) -> None:
        self._sysdescr_per_host = sysdescr_per_host or {}
        self.calls: list[Device] = []
        self.hosts_probed: list[str] = []
        self.clients: list[_WritableSnmpClientForHost] = []
        self.prior_carrier_get_calls: list[str] = []
        self._prior_carrier_khz_override = prior_carrier_khz
        self._reboot_if_required_vote_override = reboot_if_required_vote

    def __call__(self, device: Any) -> Any:
        """Behaves like ``Pmp450iSnmpDriver._client_factory(device)``.

        Returns a tiny ``WritableSnmpClient``-shaped stub that records
        the per-host sysDescr GET and the ``set`` frames emitted by
        the migration path. Issue #80: the contract is the real
        ``WritableSnmpClient`` (with ``set``); tests no longer define
        a synthetic ``apply_oid`` shim.
        """
        from typing import cast

        d = cast(Device, device)
        self.calls.append(d)
        host = str(getattr(d, "host", "?"))
        self.hosts_probed.append(host)
        client_kwargs: dict[str, Any] = dict(
            host=host,
            sysdescr=self._sysdescr_per_host.get(host, ""),
            prior_carrier_recorder=self.prior_carrier_get_calls,
        )
        if self._prior_carrier_khz_override is not None:
            client_kwargs["prior_carrier_khz"] = self._prior_carrier_khz_override
        if self._reboot_if_required_vote_override is not None:
            client_kwargs["reboot_if_required_vote"] = self._reboot_if_required_vote_override
        client = _WritableSnmpClientForHost(**client_kwargs)
        self.clients.append(client)
        return client


class _WritableSnmpClientForHost:
    """Minimal ``WritableSnmpClient``-shaped stub for issue #80.

    Implements the full ``WritableSnmpClient`` Protocol contract
    (``get_oid``, ``walk``, ``close``, ``set``). Records every
    ``set(oid, value)`` call so the wire path can be asserted in
    tests, and returns a configurable sysDescr value from
    ``get_oid``. The rollback watchdog reads
    ``migratePriorCarrierFrequency`` BEFORE the SET; we expose that
    value via ``get_oid`` when the OID matches, otherwise the
    configured sysDescr (which keeps the pre-flight path intact).

    Issue #80: this stub REPLACES the prior ``apply_oid``-shaped
    ``_SnmpClientForHost``. Defining ``apply_oid`` here would
    reproduce the bug-masking behaviour we are removing.
    """

    def __init__(
        self,
        *,
        host: str,
        sysdescr: str,
        prior_carrier_recorder: list[str] | None = None,
        prior_carrier_khz: int = 5800000,
        reboot_if_required_vote: int = 0,
    ) -> None:
        self._host = host
        self._sysdescr = sysdescr
        self._prior_carrier_khz = prior_carrier_khz
        self._prior_carrier_recorder = prior_carrier_recorder
        self._reboot_if_required_vote = reboot_if_required_vote
        self.close_calls: int = 0
        # Recorded as a list of (oid, value) tuples in call order.
        self.set_calls: list[tuple[str, str | int]] = []

    def get_oid(self, oid: str) -> str | int:
        # The pre-flight path uses sysDescr; the migration path reads
        # ``migratePriorCarrierFrequency`` (the same OID as
        # ``radioFreqCarrier`` / ``migrateCarrierFrequency``) and
        # expects an integer in kHz.
        if "10.1.1.1.1" in oid or oid.endswith("10.1.1.1.1"):
            if self._prior_carrier_recorder is not None:
                self._prior_carrier_recorder.append(oid)
            return self._prior_carrier_khz
        # rebootIfRequired (whispBoxControls 4)
        if "3.3.3.4.0" in oid or oid.endswith("3.3.3.4.0"):
            return self._reboot_if_required_vote
        return self._sysdescr

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        return []

    def set(self, oid: str, value: str | int) -> None:
        # Record (oid, value) so tests assert the wire contract.
        self.set_calls.append((oid, value))

    def close(self) -> None:
        self.close_calls += 1


def _build_driver(
    *,
    inventory: Inventory,
    registry: OidCatalogRegistry,
    factory: Callable[[Any], Any],
    settings: Settings,
) -> Any:
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver

    driver = Pmp450iSnmpDriver(
        inventory=inventory,
        catalog_registry=registry,
        client_factory=factory,
        # Issue #80: the migration path now consumes
        # ``_writable_client_factory`` for both the main SET and the
        # rollback watchdog. Inject the SAME recording factory so a
        # single test can observe which factory was reached for which
        # step and which ``set`` frames were emitted.
        writable_client_factory=factory,
    )
    driver._runtime_settings = settings
    return driver


def _settings(*, preflight_enabled: bool = True) -> Settings:
    from pydantic import SecretStr

    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_hitl_rollback_timeout_seconds=60,
        nora_hitl_signing_key=SecretStr("test-snmp-migrate-wu4-hmac-key"),
        nora_preflight_community_validation=preflight_enabled,
    )


def _fake_sm_summary(*, online_luids: tuple[str, ...], degraded_luids: tuple[str, ...]) -> Any:
    """Build a minimal ``SubscriberSummary``-shaped namespace."""
    from nora.drivers.snmp_pmp450i.subscribers import SubscriberRecord, SubscriberSummary

    online = [
        SubscriberRecord(
            luid=luid, session_uptime=86400, cinr_db=25, link_status="LINKED", modulation="8X"
        )
        for luid in online_luids
    ]
    degraded = [
        SubscriberRecord(
            luid=luid, session_uptime=43200, cinr_db=12, link_status="LINKED", modulation="2X"
        )
        for luid in degraded_luids
    ]
    return SubscriberSummary(
        target_ip="192.0.2.10",
        online_active=online,
        active_degraded=degraded,
        pre_existing_offline=[],
        baseline_size=len(online) + len(degraded),
        pre_existing_offline_count=0,
        fetched_at="2026-09-19T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Pure-helper tests — pin the IP-first / LUID-second precedence rules.
# ---------------------------------------------------------------------------


def test_resolve_sm_community_inventory_when_overrides_none() -> None:
    """``sm_communities=None`` → INVENTORY community, label INVENTORY."""
    from nora.drivers.snmp_pmp450i.migrate import _resolve_sm_community

    d = Device(
        device_id="sm-7400-001",
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        host="192.0.2.20",
        snmp_version="v2c",
        community=SecretStr("inv-community"),
    )

    community, source = _resolve_sm_community(sm_device=d, sm_luid="001", sm_communities=None)
    assert community == "inv-community"
    assert source == "INVENTORY"


def test_resolve_sm_community_inventory_when_overrides_empty_dict() -> None:
    """``sm_communities={}`` → INVENTORY community (legacy path preserved)."""
    from nora.drivers.snmp_pmp450i.migrate import _resolve_sm_community

    d = Device(
        device_id="sm-7400-001",
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        host="192.0.2.20",
        snmp_version="v2c",
        community=SecretStr("inv-community"),
    )

    community, source = _resolve_sm_community(sm_device=d, sm_luid="001", sm_communities={})
    assert community == "inv-community"
    assert source == "INVENTORY"


def test_resolve_sm_community_ip_override_wins() -> None:
    """When the SM's IP is in ``sm_communities``, OVERRIDE_IP wins."""
    from nora.drivers.snmp_pmp450i.migrate import _resolve_sm_community

    d = Device(
        device_id="sm-7400-001",
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        host="192.0.2.20",
        snmp_version="v2c",
        community=SecretStr("inv-community"),
    )

    community, source = _resolve_sm_community(
        sm_device=d,
        sm_luid="001",
        sm_communities={"192.0.2.20": "override-A"},
    )
    assert community == "override-A"
    assert source == "OVERRIDE_IP"


def test_resolve_sm_community_luid_override_wins() -> None:
    """When the LUID is in ``sm_communities`` (no IP match), OVERRIDE_LUID wins."""
    from nora.drivers.snmp_pmp450i.migrate import _resolve_sm_community

    d = Device(
        device_id="sm-7400-001",
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        host="192.0.2.20",
        snmp_version="v2c",
        community=SecretStr("inv-community"),
    )

    community, source = _resolve_sm_community(
        sm_device=d,
        sm_luid="001",
        sm_communities={"001": "override-B"},
    )
    assert community == "override-B"
    assert source == "OVERRIDE_LUID"


def test_resolve_sm_community_ip_priority_over_luid() -> None:
    """When BOTH IP and LUID are keyed, IP wins (operator decision 2026-09-19)."""
    from nora.drivers.snmp_pmp450i.migrate import _resolve_sm_community

    d = Device(
        device_id="sm-7400-001",
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        host="192.0.2.20",
        snmp_version="v2c",
        community=SecretStr("inv-community"),
    )

    community, source = _resolve_sm_community(
        sm_device=d,
        sm_luid="001",
        sm_communities={
            "192.0.2.20": "override-A",  # IP key
            "001": "override-B",  # LUID key
        },
    )
    assert community == "override-A"
    assert source == "OVERRIDE_IP"


def test_resolve_sm_community_invalid_ip_key_falls_through() -> None:
    """An IP key that does NOT match any SM falls through to inventory.

    No error, no warning — the resolver silently degrades to the
    inventory community (or to the LUID key if one is present).
    """
    from nora.drivers.snmp_pmp450i.migrate import _resolve_sm_community

    d = Device(
        device_id="sm-7400-001",
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        host="192.0.2.20",
        snmp_version="v2c",
        community=SecretStr("inv-community"),
    )

    # IP key does NOT match the SM's host; LUID key also absent.
    community, source = _resolve_sm_community(
        sm_device=d,
        sm_luid="001",
        sm_communities={"10.0.0.1": "irrelevant-override"},
    )
    assert community == "inv-community"
    assert source == "INVENTORY"

    # IP key absent, but a typo'd LUID key would also fall through to INVENTORY.
    community, source = _resolve_sm_community(
        sm_device=d,
        sm_luid="001",
        sm_communities={"001-typo": "irrelevant-override"},
    )
    assert community == "inv-community"
    assert source == "INVENTORY"


def test_resolve_sm_community_no_device_no_inventory() -> None:
    """``sm_device is None`` + no matching override → ``(None, INVENTORY)``.

    Caller treats ``(None, _)`` as "no credentials available" and
    surfaces an ``InvalidCommunity`` pre-flight failure.
    """
    from nora.drivers.snmp_pmp450i.migrate import _resolve_sm_community

    community, source = _resolve_sm_community(sm_device=None, sm_luid="001", sm_communities=None)
    assert community is None
    assert source == "INVENTORY"


def test_resolve_sm_community_luid_override_when_no_device() -> None:
    """LUID override applies even when ``sm_device`` is ``None``.

    The pre-flight short-circuits on MISSING_INVENTORY_ENTRY before
    calling the resolver, but the resolver itself tolerates
    ``sm_device=None`` so the unit-level precedence rules are
    pinned independently.
    """
    from nora.drivers.snmp_pmp450i.migrate import _resolve_sm_community

    community, source = _resolve_sm_community(
        sm_device=None,
        sm_luid="001",
        sm_communities={"001": "override-B"},
    )
    assert community == "override-B"
    assert source == "OVERRIDE_LUID"


# ---------------------------------------------------------------------------
# Pre-flight integration tests — thread overrides through
# ``_validate_sm_communities`` and inspect the resulting report.
# ---------------------------------------------------------------------------


def _run_preflight(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sm_luids: tuple[str, ...],
    sm_communities: dict[str, str] | None,
    factory: _RecordingFactory,
) -> Any:
    """Run the WU-A pre-flight with ``sm_communities`` and return the report."""
    from nora.drivers.snmp_pmp450i.migrate import _validate_sm_communities

    inv = _build_inventory_with_sms(tmp_path, sm_luids=sm_luids)
    registry = _build_catalog(firmware="15.2.1")
    settings = _settings(preflight_enabled=True)
    device = inv.get("ap-7400-01")

    # Stub the AP's sysDescr GET (the pre-flight probes AP first).
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    driver = _build_driver(inventory=inv, registry=registry, factory=factory, settings=settings)

    return _validate_sm_communities(
        driver=driver,
        device=device,
        sm_luids=list(sm_luids),
        client_factory=factory,
        sm_communities=sm_communities,
    )


def test_preflight_legacy_behaviour_preserved_when_no_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``sm_communities=None`` AND ``sm_communities={}`` → all SMs INVENTORY.

    Both shapes must keep the legacy WU-A behaviour: every SM uses
    its inventory community, the count of overrides used is zero,
    and no synthetic Device is threaded into the factory.
    """
    for overrides in (None, {}):
        factory = _RecordingFactory()
        report = _run_preflight(
            tmp_path=tmp_path,
            monkeypatch=monkeypatch,
            sm_luids=("001", "002"),
            sm_communities=overrides,
            factory=factory,
        )
        assert report.ap_reachable is True
        # Both SMs use inventory; no OVERRIDE_* labels anywhere.
        for sm in report.sm_results:
            assert sm.community_source == "INVENTORY", (
                f"Legacy overrides={overrides!r}: expected INVENTORY for every SM; "
                f"got {sm.community_source!r} on LUID {sm.luid}"
            )
        # AP probe + 2 SM probes = 3 factory calls; filter to the SM hosts.
        sm_calls = [d for d in factory.calls if d.host != "192.0.2.10"]
        assert len(sm_calls) == 2
        for device_seen in sm_calls:
            assert device_seen.community is not None
            assert device_seen.community.get_secret_value() == "change-me-v2c"


def test_preflight_ip_override_threads_override_community(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IP-keyed override → SM at that IP uses the override community.

    The factory MUST see a Device carrying the override community
    (verified via ``SecretStr.get_secret_value()``); the per-SM
    label is ``OVERRIDE_IP``.
    """
    factory = _RecordingFactory()
    report = _run_preflight(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        sm_luids=("001", "002"),
        sm_communities={"192.0.2.20": "override-A"},
        factory=factory,
    )
    assert report.ap_reachable is True
    by_luid = {sm.luid: sm for sm in report.sm_results}
    # SM 001 → 192.0.2.20 → overridden.
    assert by_luid["001"].community_source == "OVERRIDE_IP"
    assert by_luid["001"].host == "192.0.2.20"
    # SM 002 → 192.0.2.21 → no override match → INVENTORY.
    assert by_luid["002"].community_source == "INVENTORY"
    assert by_luid["002"].host == "192.0.2.21"

    # The factory saw SM 001 with the override community.
    device_for_luid_001 = next(d for d in factory.calls if d.host == "192.0.2.20")
    assert device_for_luid_001.community is not None
    assert device_for_luid_001.community.get_secret_value() == "override-A"
    # The factory saw SM 002 with the inventory community.
    device_for_luid_002 = next(d for d in factory.calls if d.host == "192.0.2.21")
    assert device_for_luid_002.community is not None
    assert device_for_luid_002.community.get_secret_value() == "change-me-v2c"


def test_preflight_luid_override_threads_override_community(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LUID-keyed override → SM with that LUID uses the override community."""
    factory = _RecordingFactory()
    report = _run_preflight(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        sm_luids=("001", "002"),
        sm_communities={"001": "override-B"},
        factory=factory,
    )
    assert report.ap_reachable is True
    by_luid = {sm.luid: sm for sm in report.sm_results}
    assert by_luid["001"].community_source == "OVERRIDE_LUID"
    assert by_luid["002"].community_source == "INVENTORY"

    device_for_luid_001 = next(d for d in factory.calls if d.host == "192.0.2.20")
    assert device_for_luid_001.community is not None
    assert device_for_luid_001.community.get_secret_value() == "override-B"


def test_preflight_ip_priority_over_luid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When both IP and LUID are keyed, IP wins (operator decision 2026-09-19)."""
    factory = _RecordingFactory()
    report = _run_preflight(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        sm_luids=("001",),
        sm_communities={
            "192.0.2.20": "override-A",  # IP key (matches SM 001)
            "001": "override-B",  # LUID key (would lose to IP)
        },
        factory=factory,
    )
    assert report.ap_reachable is True
    by_luid = {sm.luid: sm for sm in report.sm_results}
    assert by_luid["001"].community_source == "OVERRIDE_IP"

    device_for_luid_001 = next(d for d in factory.calls if d.host == "192.0.2.20")
    assert device_for_luid_001.community is not None
    assert device_for_luid_001.community.get_secret_value() == "override-A"


def test_preflight_mixed_dict_overrides_count_reflects_matches_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mixed dict: half override, half inventory.

    The aggregate ``sm_community_overrides_used`` counts only the
    SMs that actually matched (not the total keys in the dict).
    """
    from nora.drivers.snmp_pmp450i.migrate import _validate_sm_communities

    # Three SMs; only 001 and 002 are overridden by IP; 003 is not.
    inv = _build_inventory_with_sms(tmp_path, sm_luids=("001", "002", "003"))
    registry = _build_catalog(firmware="15.2.1")
    factory = _RecordingFactory()
    settings = _settings(preflight_enabled=True)
    device = inv.get("ap-7400-01")

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    driver = _build_driver(inventory=inv, registry=registry, factory=factory, settings=settings)

    report = _validate_sm_communities(
        driver=driver,
        device=device,
        sm_luids=["001", "002", "003"],
        client_factory=factory,
        sm_communities={
            "192.0.2.20": "override-A",  # SM 001 → IP match
            "192.0.2.21": "override-A2",  # SM 002 → IP match
            # SM 003 (192.0.2.22) → no override → INVENTORY.
            # LUID key typo'd to NOT match anything.
            "999": "override-irrelevant",
        },
    )

    assert report.ap_reachable is True
    by_luid = {sm.luid: sm for sm in report.sm_results}
    assert by_luid["001"].community_source == "OVERRIDE_IP"
    assert by_luid["002"].community_source == "OVERRIDE_IP"
    assert by_luid["003"].community_source == "INVENTORY"

    # Aggregate count: 2 overrides used (out of 3 SMs).
    override_count = sum(
        1 for sm in report.sm_results if sm.community_source in ("OVERRIDE_IP", "OVERRIDE_LUID")
    )
    assert override_count == 2


def test_preflight_invalid_override_key_falls_through_to_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd IP / LUID key falls through to inventory — no error."""
    factory = _RecordingFactory()
    report = _run_preflight(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        sm_luids=("001", "002"),
        sm_communities={
            "10.0.0.1": "irrelevant-override",  # no SM matches this IP
            "001-typo": "irrelevant-override",  # no SM matches this LUID
        },
        factory=factory,
    )
    assert report.ap_reachable is True
    for sm in report.sm_results:
        assert sm.community_source == "INVENTORY", (
            f"Invalid keys MUST fall through to INVENTORY; "
            f"got {sm.community_source!r} on LUID {sm.luid}"
        )
    # Aggregate count is zero.
    override_count = sum(
        1 for sm in report.sm_results if sm.community_source in ("OVERRIDE_IP", "OVERRIDE_LUID")
    )
    assert override_count == 0
    # Every device the factory saw carried the inventory community.
    for device_seen in factory.calls:
        assert device_seen.community is not None
        assert device_seen.community.get_secret_value() == "change-me-v2c"


# ---------------------------------------------------------------------------
# End-to-end test — ``fetch_migrate`` threads ``sm_communities`` and the
# resulting MigrationResult carries the aggregated override count.
# ---------------------------------------------------------------------------


def test_fetch_migrate_threads_sm_communities_to_migration_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: ``fetch_migrate`` aggregates overrides on the returned dict.

    Stub the downstream migration loop so the test focuses on the
    pre-flight → MigrationResult path; assert that
    ``sm_community_overrides_used`` reflects the count of overrides
    that actually matched a candidate SM.
    """
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    inv = _build_inventory_with_sms(tmp_path, sm_luids=("001", "002"))
    registry = _build_catalog(firmware="15.2.1")
    factory = _RecordingFactory(
        sysdescr_per_host={
            "192.0.2.10": "Cambium PMP 450i AP 15.2.1",
            "192.0.2.20": "Cambium PMP 450i SM 001 15.2.1",
            "192.0.2.21": "Cambium PMP 450i SM 002 15.2.1",
        }
    )
    settings = _settings(preflight_enabled=True)
    driver = _build_driver(inventory=inv, registry=registry, factory=factory, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        migrate_mod,
        "fetch_sm_table",
        lambda **kw: _fake_sm_summary(online_luids=("001", "002"), degraded_luids=()),
    )
    # Stub the migration downstream so the test does not exercise
    # the SET path / watchdog / HITL gate.
    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", lambda **kw: None)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "_wait_for_management_reachability", lambda **kw: True)

    # Capture the intervention record payload to assert the audit
    # trail records the override count.
    captured_records: list[dict[str, Any]] = []

    def fake_save(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured_records.append({"args": args, "kwargs": kwargs})
        return {"status": "OK", "intervention_id": "INT-test-wu4"}

    monkeypatch.setattr(migrate_mod, "save_intervention_record", fake_save)

    # Mint a valid token so the gate clears (we just want the count).
    import json

    from nora.hitl.tokens import mint_token

    token_obj = mint_token(
        "tester",
        ttl_seconds=900,
        signing_key=SecretStr("test-snmp-migrate-wu4-hmac-key"),
    )
    valid_token = json.dumps(token_obj.model_dump(mode="json"))

    # Half the SMs overridden by IP; SM 002 falls through to inventory.
    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=5800.0,
        settings=settings,
        sm_communities={"192.0.2.20": "override-A"},  # SM 001 only
    )

    # Aggregate count: exactly one SM used the override.
    assert result["sm_community_overrides_used"] == 1, (
        f"Expected exactly 1 override used; got {result!r}"
    )
    # Existing fields preserved.
    assert result["rolled_back"] is False
    assert result["target_frequency_mhz"] == 5800.0
    assert result["device_id"] == "192.0.2.10"
    assert result["online_active_migrated"] == 2
    assert result["active_degraded_migrated"] == 0
    assert result["pre_existing_offline_excluded"] == 0
    # Issue #80: recording client implements the WritableSnmpClient
    # Protocol (with ``set``); the real-SET path runs and emits the
    # AP carrier-change SET on the wire. ``dry_run=False`` and
    # ``would_set`` is empty because every SET went through the
    # recording stub instead of being deferred.
    assert result["dry_run"] is False
    assert result["would_set"] == []

    # The intervention record also carries the override count.
    assert len(captured_records) == 1
    record = captured_records[0]["args"][1]
    assert record["sm_community_overrides_used"] == 1
    # The community string itself MUST NOT be in the audit record.
    record_text = json.dumps(record)
    assert "override-A" not in record_text, (
        "Community string MUST NOT appear in the audit record; only the source label travels in"
    )


# ---------------------------------------------------------------------------
# Issue #80 — wire-path regression tests.
#
# These tests pin the Tier-1 contract: when an operator authorizes a
# migration with a valid HITL token, the helper MUST emit an SNMP
# SET frame against the AP carrier frequency OID with the value
# converted to kHz (per Cambium WHISP-APS-MIB for
# ``whispApsRFConfigRadioEntry.radioFreqCarrier``). The pre-fix
# code fell back to a typed ``dry_run=True`` because the gate
# referenced a method that never existed on the production client.
# ---------------------------------------------------------------------------


def test_fetch_migrate_emits_set_with_khz_unit_on_real_wire(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #80: ``fetch_migrate`` MUST emit a real SET frame.

    Pins three contracts:

    1. The driver opens the client via ``_writable_client_factory``
       (NOT the read-only ``_client_factory``) for the main path.
    2. The gate ``hasattr(client, "set")`` is True for a
       WritableSnmpClient-shaped client; the dry-run fallback does
       NOT fire.
    3. The wire SET carries the carrier frequency in kHz
       (``int(round(target_frequency_mhz * 1000))``) for the
       ``migrateCarrierFrequency`` OID.

    Pre-fix this test fails on the first assertion because the
    buggy code routed through ``_client_factory`` (read-only),
    which returns a stub WITHOUT ``set``, falling back to dry-run.
    """
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    inv = _build_inventory_with_sms(tmp_path, sm_luids=())
    registry = _build_catalog(firmware="15.2.1")
    factory = _RecordingFactory(
        sysdescr_per_host={"192.0.2.10": "Cambium PMP 450i AP 15.2.1"}
    )
    settings = _settings(preflight_enabled=True)
    driver = _build_driver(inventory=inv, registry=registry, factory=factory, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        migrate_mod,
        "fetch_sm_table",
        lambda **kw: _fake_sm_summary(online_luids=(), degraded_luids=()),
    )
    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", lambda **kw: None)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "_wait_for_management_reachability", lambda **kw: True)
    monkeypatch.setattr(migrate_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    import json

    from nora.hitl.tokens import mint_token

    token_obj = mint_token(
        "tester",
        ttl_seconds=900,
        signing_key=SecretStr("test-snmp-migrate-wu4-hmac-key"),
    )
    valid_token = json.dumps(token_obj.model_dump(mode="json"))

    target_mhz = 5800.0
    expected_khz = int(round(target_mhz * 1000))  # 5_800_000
    migrate_oid = "1.3.6.1.4.1.161.19.3.1.10.1.1.1.1"

    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=target_mhz,
        settings=settings,
    )

    # Contract 1: dry-run did NOT fire.
    assert result["dry_run"] is False, (
        f"Expected the real-SET path; got dry_run=True. would_set={result['would_set']!r}"
    )
    assert result["would_set"] == []

    # Contract 2 + 3: at least one of the recording clients emitted a
    # ``set`` against the migrate OID with the kHz value. The
    # migration path opens the client via ``_writable_client_factory``,
    # which (in this test) is the same recording factory — so we
    # aggregate ``set_calls`` across all clients the factory returned.
    all_set_calls = [call for client in factory.clients for call in client.set_calls]
    matching = [
        (oid, value)
        for oid, value in all_set_calls
        if oid == migrate_oid and value == expected_khz
    ]
    assert matching, (
        f"Expected at least one ``set({migrate_oid!r}, {expected_khz})`` on the wire; "
        f"got set_calls={all_set_calls!r}. "
        f"This is the issue #80 bug: production falls back to dry-run because "
        f"the gate references a method that does not exist."
    )


def test_fetch_migrate_rollback_watchdog_uses_writable_client_and_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #80: rollback watchdog MUST use the writable factory too.

    Pins two contracts:

    1. The factory called inside ``_on_loss_of_management`` is the
       WRITABLE factory (a fresh client is opened via
       ``_writable_client_factory`` so the rollback SET actually
       fires).
    2. The rollback SET carries the prior-carrier value in kHz
       (passthrough from the GET — the prior read is already in
       kHz per Cambium WHISP-APS-MIB).

    We capture the closure passed to ``_start_rollback_watchdog``
    and invoke it manually so the test is deterministic (no real
    timer is armed, no management-reachability race).
    """
    import json

    from nora.drivers.snmp_pmp450i import migrate as migrate_mod
    from nora.hitl.tokens import mint_token

    inv = _build_inventory_with_sms(tmp_path, sm_luids=())
    registry = _build_catalog(firmware="15.2.1")
    factory = _RecordingFactory(
        sysdescr_per_host={"192.0.2.10": "Cambium PMP 450i AP 15.2.1"},
        # Issue #80: drive the prior_carrier GET response from the
        # factory seam itself. Every client minted by ``__call__``
        # (including the one opened by the rollback watchdog) will
        # return 5785000 kHz for the carrier OID, so the
        # ``prior_carrier`` capture inside `fetch_migrate` AND the
        # subsequent rollback SET both carry the value the test
        # asserts against. No post-hoc mutation is needed.
        prior_carrier_khz=5785000,
    )
    settings = _settings(preflight_enabled=True)
    driver = _build_driver(inventory=inv, registry=registry, factory=factory, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        migrate_mod,
        "fetch_sm_table",
        lambda **kw: _fake_sm_summary(online_luids=(), degraded_luids=()),
    )

    captured_closure: dict[str, Any] = {}

    def _capture_rollback_closure(**kwargs: Any) -> Any:
        # Capture the closure ``_on_loss_of_management`` so the test
        # can invoke it deterministically. The timer token returned
        # by the real function is unused here.
        captured_closure["on_loss_of_management"] = kwargs["on_loss_of_management"]
        return None

    monkeypatch.setattr(
        migrate_mod, "_start_rollback_watchdog", _capture_rollback_closure
    )
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "_wait_for_management_reachability", lambda **kw: True)
    monkeypatch.setattr(migrate_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    token_obj = mint_token(
        "tester",
        ttl_seconds=900,
        signing_key=SecretStr("test-snmp-migrate-wu4-hmac-key"),
    )
    valid_token = json.dumps(token_obj.model_dump(mode="json"))

    prior_khz = 5785000  # starting carrier, in kHz (Cambium MIB unit)
    migrate_oid = "1.3.6.1.4.1.161.19.3.1.10.1.1.1.1"

    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=5800.0,
        settings=settings,
    )

    # The real-SET path ran (gate fires because the stub defines
    # ``set``), the watchdog closure was captured, and the main SET
    # was emitted on the wire.
    assert result["dry_run"] is False
    assert result["would_set"] == []

    # Manually invoke the rollback closure. After the fix, this MUST
    # open a fresh client via ``_writable_client_factory`` and emit
    # ``set(migrate_oid, prior_khz)`` on it.
    assert "on_loss_of_management" in captured_closure, (
        "Watchdog closure was never captured — the SET path did not arm "
        "the watchdog. Check the gate (``hasattr(client, 'set')``)."
    )
    captured_closure["on_loss_of_management"]()

    # Look for the rollback SET across every factory-issued client.
    rollback_set_calls = []
    for client in factory.clients:
        for oid, value in client.set_calls:
            if oid == migrate_oid and value == prior_khz:
                rollback_set_calls.append((oid, value))
    assert rollback_set_calls, (
        f"Expected at least one rollback ``set({migrate_oid!r}, {prior_khz})``; "
        f"got set_calls={[(c.set_calls) for c in factory.clients]!r}. "
        f"This is the issue #80 watchdog variant: the rollback falls back to "
        f"dry-run because the gate references a non-existent method AND the "
        f"rollback client is opened via the read-only factory."
    )


def test_fetch_migrate_dry_run_falls_back_when_client_lacks_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry-run seam stays: a client WITHOUT ``set`` falls back.

    Issue #80: the fix preserves the WU-3 dry-run contract — a
    client lacking the write verb (e.g. a thin read-only mock
    used by upstream tests that do NOT want to exercise SET
    frames) MUST still surface ``dry_run=True`` with the
    would-be SET pair in ``would_set``. Only the GATE reference
    changes from ``apply_oid`` (a non-existent verb) to ``set``
    (the real WritableSnmpClient verb).
    """
    import json

    from nora.drivers.snmp_pmp450i import migrate as migrate_mod
    from nora.hitl.tokens import mint_token

    class _ReadOnlyFactory:
        """Returns a client that satisfies ``SnmpClient`` (read-only)."""

        def __init__(self) -> None:
            self.clients: list[_ReadOnlyClient] = []

        def __call__(self, device: Any) -> Any:
            client = _ReadOnlyClient(host=str(getattr(device, "host", "?")))
            self.clients.append(client)
            return client

    class _ReadOnlyClient:
        def __init__(self, *, host: str) -> None:
            self._host = host
            self.close_calls = 0

        def get_oid(self, oid: str) -> str | int:
            # Pre-flight sysDescr returns a string so AP reachability
            # succeeds; the migration path's GET of prior_carrier
            # returns an integer kHz.
            if "1.3.6.1.4.1.161.19.3.1.10" in oid:
                return 5785000
            return "Cambium PMP 450i AP 15.2.1"

        def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
            return []

        def close(self) -> None:
            self.close_calls += 1

    ro_factory = _ReadOnlyFactory()
    inv = _build_inventory_with_sms(tmp_path, sm_luids=())
    registry = _build_catalog(firmware="15.2.1")
    settings = _settings(preflight_enabled=True)
    driver = _build_driver(inventory=inv, registry=registry, factory=ro_factory, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        migrate_mod,
        "fetch_sm_table",
        lambda **kw: _fake_sm_summary(online_luids=(), degraded_luids=()),
    )
    monkeypatch.setattr(migrate_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    token_obj = mint_token(
        "tester",
        ttl_seconds=900,
        signing_key=SecretStr("test-snmp-migrate-wu4-hmac-key"),
    )
    valid_token = json.dumps(token_obj.model_dump(mode="json"))

    target_mhz = 5800.0
    # Issue #80: the ``_build_catalog`` helper now mirrors the
    # production catalog, which points ``migrateCarrierFrequency``
    # at the real Cambium WHISP-APS-MIB OID
    # ``whispApsRFConfigRadioEntry.radioFreqCarrier`` (radioIndex=1):
    # ``1.3.6.1.4.1.161.19.3.1.10.1.1.1.1``. The dry-run contract
    # is what the helper WOULD have SET against the OID the catalog
    # resolved at runtime.
    migrate_oid = "1.3.6.1.4.1.161.19.3.1.10.1.1.1.1"

    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=target_mhz,
        settings=settings,
    )

    # Dry-run fired; would_set carries the would-be SET pair.
    # Pydantic v2 serializes the inner tuple as a list when the
    # field is accessed via ``__getitem__``; we assert the list shape
    # the runtime actually returns.
    assert result["dry_run"] is True
    assert result["would_set"] == [[migrate_oid, target_mhz]]
    assert result["rolled_back"] is False


def test_fetch_migrate_reboot_required_when_firmware_votes_1(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #84: MigrationResult carries reboot_required=True when rebootIfRequired is 1."""
    import json

    from nora.drivers.snmp_pmp450i import migrate as migrate_mod
    from nora.hitl.tokens import mint_token

    inv = _build_inventory_with_sms(tmp_path, sm_luids=())
    registry = _build_catalog(firmware="15.2.1")
    factory = _RecordingFactory(reboot_if_required_vote=1)
    settings = _settings(preflight_enabled=True)
    driver = _build_driver(inventory=inv, registry=registry, factory=factory, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        migrate_mod,
        "fetch_sm_table",
        lambda **kw: _fake_sm_summary(online_luids=(), degraded_luids=()),
    )
    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", lambda **kw: None)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "_wait_for_management_reachability", lambda **kw: True)
    monkeypatch.setattr(migrate_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    token_obj = mint_token(
        "tester",
        ttl_seconds=900,
        signing_key=SecretStr("test-snmp-migrate-wu4-hmac-key"),
    )
    valid_token = json.dumps(token_obj.model_dump(mode="json"))

    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=5800.0,
        settings=settings,
    )

    assert result["dry_run"] is False
    assert result["reboot_required"] is True


def test_fetch_migrate_reboot_required_false_when_firmware_votes_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #84: MigrationResult carries reboot_required=False when rebootIfRequired is 0."""
    import json

    from nora.drivers.snmp_pmp450i import migrate as migrate_mod
    from nora.hitl.tokens import mint_token

    inv = _build_inventory_with_sms(tmp_path, sm_luids=())
    registry = _build_catalog(firmware="15.2.1")
    factory = _RecordingFactory(reboot_if_required_vote=0)
    settings = _settings(preflight_enabled=True)
    driver = _build_driver(inventory=inv, registry=registry, factory=factory, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        migrate_mod,
        "fetch_sm_table",
        lambda **kw: _fake_sm_summary(online_luids=(), degraded_luids=()),
    )
    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", lambda **kw: None)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "_wait_for_management_reachability", lambda **kw: True)
    monkeypatch.setattr(migrate_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    token_obj = mint_token(
        "tester",
        ttl_seconds=900,
        signing_key=SecretStr("test-snmp-migrate-wu4-hmac-key"),
    )
    valid_token = json.dumps(token_obj.model_dump(mode="json"))

    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=5800.0,
        settings=settings,
    )

    assert result["dry_run"] is False
    assert result["reboot_required"] is False


__all__ = [
    # Pure-helper tests — precedence rules.
    "test_resolve_sm_community_inventory_when_overrides_none",
    "test_resolve_sm_community_inventory_when_overrides_empty_dict",
    "test_resolve_sm_community_ip_override_wins",
    "test_resolve_sm_community_luid_override_wins",
    "test_resolve_sm_community_ip_priority_over_luid",
    "test_resolve_sm_community_invalid_ip_key_falls_through",
    "test_resolve_sm_community_no_device_no_inventory",
    "test_resolve_sm_community_luid_override_when_no_device",
    # Pre-flight integration tests.
    "test_preflight_legacy_behaviour_preserved_when_no_overrides",
    "test_preflight_ip_override_threads_override_community",
    "test_preflight_luid_override_threads_override_community",
    "test_preflight_ip_priority_over_luid",
    "test_preflight_mixed_dict_overrides_count_reflects_matches_only",
    "test_preflight_invalid_override_key_falls_through_to_inventory",
    # End-to-end test (legacy WU-4 contract).
    "test_fetch_migrate_threads_sm_communities_to_migration_result",
    # Issue #80 wire-path regression tests.
    "test_fetch_migrate_emits_set_with_khz_unit_on_real_wire",
    "test_fetch_migrate_rollback_watchdog_uses_writable_client_and_set",
    "test_fetch_migrate_dry_run_falls_back_when_client_lacks_set",
    # Issue #84 reboot_required regression tests.
    "test_fetch_migrate_reboot_required_when_firmware_votes_1",
    "test_fetch_migrate_reboot_required_false_when_firmware_votes_0",
]
