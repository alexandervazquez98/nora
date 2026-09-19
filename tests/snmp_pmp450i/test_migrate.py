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
    """Minimal catalog registry covering the radio / SM / migration OIDs."""
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
        "migrateCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.4.1.38.0",
        "migratePriorCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.4.1.38.0",
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
    """

    def __init__(self, *, sysdescr_per_host: dict[str, str] | None = None) -> None:
        self._sysdescr_per_host = sysdescr_per_host or {}
        self.calls: list[Device] = []
        self.hosts_probed: list[str] = []

    def __call__(self, device: Any) -> Any:
        """Behaves like ``Pmp450iSnmpDriver._client_factory(device)``.

        Returns a tiny ``SnmpClient``-shaped stub that records the
        per-host sysDescr GET; we close it eagerly.
        """
        from typing import cast

        d = cast(Device, device)
        self.calls.append(d)
        host = str(getattr(d, "host", "?"))
        self.hosts_probed.append(host)
        return _SnmpClientForHost(host=host, sysdescr=self._sysdescr_per_host.get(host, ""))


class _SnmpClientForHost:
    """Minimal SnmpClient-shaped stub: ``get_oid`` returns the per-host sysDescr."""

    def __init__(self, *, host: str, sysdescr: str) -> None:
        self._host = host
        self._sysdescr = sysdescr
        self.close_calls: int = 0

    def get_oid(self, oid: str) -> str | int:
        # The pre-flight only ever calls ``get_oid`` with sysDescr.
        return self._sysdescr

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        return []

    def apply_oid(self, oid: str, value: str | int) -> None:
        return None

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
    # Recording client defines ``apply_oid`` so the real-SET path
    # runs; ``dry_run=False`` and ``would_set`` is empty.
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
    # End-to-end test.
    "test_fetch_migrate_threads_sm_communities_to_migration_result",
]
