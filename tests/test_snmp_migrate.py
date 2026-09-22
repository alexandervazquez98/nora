"""Tests for the HITL-gated RF migration tool — PR 4 slice 4 commit 3.

These tests pin the public contract for the slice-4
``snmp_migrate_radio_frequency`` MCP tool:

* HITL gate — missing or invalid approval tokens raise
  :class:`AutonomousMutationRejected` with the **literal** message
  ``"autonomous device mutation rejected: HITL approval token
  required"``. The verifier call site is the contract seam.
* Make-before-break — ONLINE_ACTIVE subscribers migrate BEFORE
  ACTIVE_DEGRADED subscribers; AP channel change runs LAST.
  PRE_EXISTING_OFFLINE SMs are excluded by the central
  :func:`categorize_subscribers` helper.
* Rollback watchdog — a ``threading.Timer`` armed after the AP SET
  frame reverts the carrier frequency on loss-of-management and
  emits a ``POST_MIGRATION`` intervention record with
  ``rolled_back: true``.
* Intervention record emission — the tool emits exactly one
  ``save_intervention_record`` call per completion (success or
  rollback).

Named tests for PR 4 (slice 4 commit 3):

* ``test_migrate_requires_hitl_approval_token``
* ``test_migrate_make_before_break_migrates_online_active_first``
* ``test_migrate_excludes_pre_existing_offline_subscribers``
* ``test_migrate_rolls_back_within_timeout_on_loss_of_management``
* ``test_migrate_autonomous_call_raises_autonomous_mutation_rejected``
* ``test_migrate_emits_intervention_record_on_completion``

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real
IPs, hostnames, serials, or credentials.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from nora.config import Settings
from nora.drivers.inventory import Inventory
from nora.drivers.oid_catalog import OidCatalog, OidCatalogRegistry

# ---------------------------------------------------------------------------
# Helpers — inventory + catalog + fake client.
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
    """The six radio-metrics REQUIRED_OIDs (issue #54 rename).

    Issue #54 (2026-09-19): ``signalStrengthTx`` (the broken
    ``maxSMTxPwr`` engineering-only + tabular OID) was replaced by
    ``eirp`` (``whispBoxActiveEIRP``, ``.306.0``).
    """
    return {
        "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.4.1.36.0",
        "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.4.1.38.0",
        "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.4.1.34.0",
        "eirp": "1.3.6.1.4.1.161.19.3.3.1.306.0",
        "ssr": "1.3.6.1.4.1.161.19.3.1.4.1.86.0",
        "modulationMode": "1.3.6.1.4.1.161.19.3.1.4.1.40.0",
    }


def _sm_table_oids() -> dict[str, str]:
    """SM table OID names + dotted OIDs (slice 3 additions).

    Issue #69 (2026-09-20): ``smSiteName`` (``whispLinkEntry.33`` =
    ``linkSiteName``, ``DisplayString``) and ``smIpAddress``
    (``whispLinkEntry.69`` = ``linkIpAddress``, ``IpAddress``) are
    required by the schema gate in
    :func:`_build_sm_table_column_map` after the WU-1 extension of
    ``SM_TABLE_OID_NAMES``. Operator-verified via live ``snmpwalk``
    against production APs (firmwares 25.0.1 / 25.1.0).
    """
    return {
        "smSessionUptime": "1.3.6.1.4.1.161.19.3.1.4.1.46.0",
        "smCinr": "1.3.6.1.4.1.161.19.3.1.4.1.74.0",
        "smLinkStatus": "1.3.6.1.4.1.161.19.3.1.4.1.19.0",
        "smLuid": "1.3.6.1.4.1.161.19.3.1.4.1.1.0",
        "smSiteName": "1.3.6.1.4.1.161.19.3.1.4.1.33.0",
        "smIpAddress": "1.3.6.1.4.1.161.19.3.1.4.1.69.0",
    }


def _migration_oids() -> dict[str, str]:
    """Migration OID names + dotted OIDs (slice 4 PR 4 commit 3).

    Per Cambium private-enterprise branch
    ``1.3.6.1.4.1.161.19.3.x.x.0``; indices 95-96 to avoid colliding
    with the existing seed.
    """
    return {
        "migrateCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.4.1.38.0",
        "migratePriorCarrierFrequency": "1.3.6.1.4.1.161.19.3.1.4.1.38.0",
    }


def _build_catalog(
    firmware: str = "15.2.1",
    *,
    include_migration_oids: bool = True,
) -> OidCatalogRegistry:
    """Catalog registry carrying the radio seed + SM + (optional) migration OIDs."""
    oids: dict[str, str] = {}
    oids.update(_radio_seed_oids())
    oids.update(_sm_table_oids())
    if include_migration_oids:
        oids.update(_migration_oids())
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


def _sm_subtree_rows(
    *,
    online_luids: list[str],
    degraded_luids: list[str],
    pre_existing_luids: list[str],
) -> list[tuple[str, str | int]]:
    """Build a synthetic SM-table subtree response covering three categories.

    Each SM occupies six OID rows in the public Cambium branch
    (smSessionUptime, smCinr, smLinkStatus, smLuid, smSiteName,
    smIpAddress). ONLINE_ACTIVE rows carry uptime > 0 AND inSession
    (``linkSessState = 1``) AND healthy CINR. ACTIVE_DEGRADED rows
    carry uptime > 0 AND inSession AND CINR < 18.
    PRE_EXISTING_OFFLINE rows carry uptime == 0 (the central
    categoriser treats this as pre-existing offline even without
    history).

    Issue #58 (2026-09-19): ``linkSessState`` is an INTEGER enum per
    WHISP-APS-MIB (``idle=0``, ``inSession=1``, ``clearing=2``, ...).
    The fixture used to emit the legacy ``"LINKED"`` / ``"DOWN"``
    strings — the radio never returns those. ``1`` is the active
    value; ``0`` is the offline value.

    Issue #69 (2026-09-20): every SM also seeds ``linkSiteName``
    (``.33``) and ``linkIpAddress`` (``.69``) so the WU-1 schema
    extension (``SM_TABLE_OID_NAMES`` now requires all six names)
    is satisfied. IPs use TEST-NET-1 (``192.0.2.x``) to stay
    Zero-Leakage. PRE_EXISTING_OFFLINE SMs also carry real IPs —
    the migrate tests do not exercise the ``0.0.0.0`` heuristic
    (the ``session_uptime == 0`` trigger already routes them to
    PRE_EXISTING_OFFLINE), so a real IP keeps the fixture honest
    without overloading the heuristic semantics.
    """
    base = "1.3.6.1.4.1.161.19.3.1.4.1"
    rows: list[tuple[str, str | int]] = []
    sm_index = 1
    for luid in online_luids:
        rows.extend(
            [
                (f"{base}.46.{sm_index}", 86400),
                (f"{base}.74.{sm_index}", 25),
                (f"{base}.19.{sm_index}", 1),  # inSession
                (f"{base}.1.{sm_index}", luid),
                (f"{base}.33.{sm_index}", f"BAJ02-VVU-TIJU-{17 + sm_index:03d}"),
                (f"{base}.69.{sm_index}", f"192.0.2.{30 + sm_index}"),
            ]
        )
        sm_index += 1
    for luid in degraded_luids:
        rows.extend(
            [
                (f"{base}.46.{sm_index}", 43200),
                (f"{base}.74.{sm_index}", 12),
                (f"{base}.19.{sm_index}", 1),  # inSession (degraded signal)
                (f"{base}.1.{sm_index}", luid),
                (f"{base}.33.{sm_index}", f"BAJ02-VVU-TIJU-{17 + sm_index:03d}"),
                (f"{base}.69.{sm_index}", f"192.0.2.{30 + sm_index}"),
            ]
        )
        sm_index += 1
    for luid in pre_existing_luids:
        rows.extend(
            [
                (f"{base}.46.{sm_index}", 0),
                (f"{base}.74.{sm_index}", 0),
                (f"{base}.19.{sm_index}", 0),  # idle
                (f"{base}.1.{sm_index}", luid),
                (f"{base}.33.{sm_index}", f"BAJ02-VVU-TIJU-{17 + sm_index:03d}"),
                (f"{base}.69.{sm_index}", f"192.0.2.{30 + sm_index}"),
            ]
        )
        sm_index += 1
    return rows


class _FakeSnmpClient:
    """Fake client — returns canned values keyed by dotted OID."""

    def __init__(
        self,
        values: dict[str, str | int],
        *,
        walk_results: dict[str, list[tuple[str, str | int]]] | None = None,
        set_calls: list[str] | None = None,
    ) -> None:
        self._values = dict(values)
        self._walk_results: dict[str, list[tuple[str, str | int]]] = walk_results or {}
        self.get_calls: list[str] = []
        self.walk_calls: list[str] = []
        # SET frame capture — production SnmpClient is read-only, so
        # the fake's ``apply_oid`` was the seam for the AP channel
        # change. Issue #80 (PR #80 follow-up): the production gate
        # moved from ``apply_oid`` to the ``WritableSnmpClient``
        # ``set`` verb. The fake now exposes both for back-compat:
        # ``apply_oid`` keeps legacy assertions working, ``set`` is
        # the verb the production helper actually calls.
        self._set_calls: list[str] = set_calls if set_calls is not None else []

    def get_oid(self, oid: str) -> str | int:
        self.get_calls.append(oid)
        if oid not in self._values:
            raise KeyError(oid)
        return self._values[oid]

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        self.walk_calls.append(base_oid)
        return list(self._walk_results.get(base_oid, []))

    def apply_oid(self, oid: str, value: str | int) -> None:
        """Apply a write frame — kept for legacy tests that pin the old seam.

        Issue #80: production code now reaches the ``set`` verb
        instead. New assertions should prefer ``set_calls``; this
        method remains so legacy assertions that check
        ``canned._set_calls`` for backwards-compat reasons keep
        working.
        """
        self._set_calls.append(f"{oid}={value}")

    def set(self, oid: str, value: str | int) -> None:
        """Issue #80: ``WritableSnmpClient`` Protocol verb.

        The production gate is ``hasattr(client, "set")``; this
        method makes the fake satisfy the new contract so the
        real-SET path runs against the fake (not the production
        ``WritableV2CClient``, which would time out on the real
        network).
        """
        self._set_calls.append(f"{oid}={value}")

    def close(self) -> None:
        return None


def _build_driver(
    *,
    inventory: Inventory,
    registry: OidCatalogRegistry,
    canned: _FakeSnmpClient,
    settings: Settings | None = None,
) -> Any:
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver

    driver = Pmp450iSnmpDriver(
        inventory=inventory,
        catalog_registry=registry,
        client_factory=lambda d: canned,
        # Issue #80: wire the same canned fake through the
        # writable-factory seam so the production helper's
        # ``driver._writable_client_factory(device)`` call (which
        # replaced ``_client_factory`` for the migration SET path
        # and the rollback watchdog) reaches the fake instead of
        # the default ``WritableV2CClient``. Without this, the
        # legacy tests would attempt real-wire SETs against
        # ``192.0.2.x`` and time out after 5.0s.
        writable_client_factory=lambda d: canned,
    )
    if settings is not None:
        driver._runtime_settings = settings
    return driver


def _mint_valid_token(operator_id: str = "tester") -> str:
    """Mint a stub approval token + JSON-encode it for the verifier."""
    from pydantic import SecretStr

    from nora.hitl.tokens import mint_token

    token_obj = mint_token(
        operator_id,
        ttl_seconds=900,
        signing_key=SecretStr("test-snmp-migrate-hmac-key"),
    )
    return json.dumps(token_obj.model_dump(mode="json"))


def _settings_with_rollback_timeout(
    seconds: int,
    *,
    preflight_community_validation: bool = False,
) -> Settings:
    from pydantic import SecretStr

    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_hitl_rollback_timeout_seconds=seconds,
        nora_hitl_signing_key=SecretStr("test-snmp-migrate-hmac-key"),
        # Legacy test fixtures (PR #44 slice 4) predate WU-A. The
        # default is to opt them OUT of the pre-flight so the
        # unchanged tests keep exercising the existing contract.
        # WU-A's own tests (test_preflight_*) override this to True.
        nora_preflight_community_validation=preflight_community_validation,
    )


# ---------------------------------------------------------------------------
# Named test #1 — migrate_requires_hitl_approval_token
# ---------------------------------------------------------------------------


def test_migrate_requires_hitl_approval_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fetch_migrate`` raises ``AutonomousMutationRejected`` on missing/invalid tokens.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 3 requirement
    "Approval Token Contract": the migration tool MUST call
    :func:`nora.hitl.tokens.verify_approval_token` FIRST; missing or
    invalid tokens raise the literal-typed exception with the
    **literal** message ``"autonomous device mutation rejected:
    HITL approval token required"``. No SNMP SET frame is emitted.
    """
    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.drivers.snmp_pmp450i.migrate import fetch_migrate

    expected = "autonomous device mutation rejected: HITL approval token required"

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    canned = _FakeSnmpClient(
        values={},
        walk_results={
            "1.3.6.1.4.1.161.19.3.1.4.1": _sm_subtree_rows(
                online_luids=["001"],
                degraded_luids=["002"],
                pre_existing_luids=["003"],
            ),
        },
    )
    settings = _settings_with_rollback_timeout(60)
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    # No history so PRE_EXISTING_OFFLINE is excluded only via uptime==0.
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    # Path 1: missing token (None).
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        fetch_migrate(
            driver=driver,
            device_id="ap-7400-01",
            approval_token=None,
            target_frequency_mhz=5800.0,
            settings=settings,
        )
    assert str(exc_info.value) == expected

    # Path 2: invalid token (empty string).
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        fetch_migrate(
            driver=driver,
            device_id="ap-7400-01",
            approval_token="",
            target_frequency_mhz=5800.0,
            settings=settings,
        )
    assert str(exc_info.value) == expected

    # Path 3: invalid token (non-JSON garbage).
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        fetch_migrate(
            driver=driver,
            device_id="ap-7400-01",
            approval_token="expired-or-bogus",
            target_frequency_mhz=5800.0,
            settings=settings,
        )
    assert str(exc_info.value) == expected

    # Zero SET frames emitted — the gate fires before any wire mutation.
    assert canned._set_calls == [], (
        f"migrate MUST NOT emit SET frames when the approval token is rejected; "
        f"got {canned._set_calls!r}"
    )


# ---------------------------------------------------------------------------
# Named test #2 — migrate_make_before_break_migrates_online_active_first
# ---------------------------------------------------------------------------


def test_migrate_make_before_break_migrates_online_active_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Make-before-break order: ONLINE_ACTIVE → ACTIVE_DEGRADED → AP.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 3 requirement
    "Make-Before-Break Order + PRE_EXISTING_OFFLINE Exclusion":
    migration order is (1) ONLINE_ACTIVE, (2) ACTIVE_DEGRADED,
    (3) AP channel change LAST. The test spies on the
    ``migrate_subscribers`` call sequence to assert the order.
    """
    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    canned = _FakeSnmpClient(
        values={},
        walk_results={
            "1.3.6.1.4.1.161.19.3.1.4.1": _sm_subtree_rows(
                online_luids=["001", "002"],
                degraded_luids=["003"],
                pre_existing_luids=["004"],
            ),
        },
    )
    settings = _settings_with_rollback_timeout(60)
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    # No history so PRE_EXISTING_OFFLINE is excluded only via uptime==0.
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    # Spy on ``migrate_subscribers`` so we can assert the order.
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    call_log: list[str] = []

    def spy_migrate_subscribers(
        *,
        category: str,
        luid: str,
        target_frequency_mhz: float,
        client: Any,
    ) -> dict[str, Any]:
        call_log.append(f"{category}:{luid}")
        return {"status": "OK", "luid": luid, "category": category}

    monkeypatch.setattr(migrate_mod, "migrate_subscriber", spy_migrate_subscribers)

    # Stub the watchdog so the test does not sleep — fires immediately,
    # reports ``rolled_back: False`` so we can observe the success path.
    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", lambda **kw: None)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "_wait_for_management_reachability", lambda **kw: True)
    monkeypatch.setattr(
        migrate_mod,
        "save_intervention_record",
        lambda *a, **kw: {
            "status": "OK",
            "intervention_id": "INT-test",
        },
    )

    valid_token = _mint_valid_token()
    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=5800.0,
        settings=settings,
    )

    # ONLINE_ACTIVE comes first, ACTIVE_DEGRADED second, AP carrier LAST.
    assert call_log == [
        "ONLINE_ACTIVE:001",
        "ONLINE_ACTIVE:002",
        "ACTIVE_DEGRADED:003",
    ], f"Expected make-before-break order; got {call_log}"

    # AP SET frame is the LAST wire mutation.
    assert len(canned._set_calls) == 1, (
        f"Expected exactly 1 AP SET frame; got {canned._set_calls!r}"
    )
    migration_freq_oid = "1.3.6.1.4.1.161.19.3.1.4.1.38.0="
    assert canned._set_calls[0].startswith(migration_freq_oid)

    assert result["rolled_back"] is False


# ---------------------------------------------------------------------------
# Named test #3 — migrate_excludes_pre_existing_offline_subscribers
# ---------------------------------------------------------------------------


def test_migrate_excludes_pre_existing_offline_subscribers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PRE_EXISTING_OFFLINE SMs never receive a migration SET frame.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 3 scenario
    "ONLINE_ACTIVE first; AP last; PRE_EXISTING_OFFLINE excluded":
    the central :func:`categorize_subscribers` helper excludes
    PRE_EXISTING_OFFLINE SMs from the migration candidate set.
    The test spies on ``migrate_subscriber`` and asserts it was
    NEVER called with ``category='PRE_EXISTING_OFFLINE'``.
    """
    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    canned = _FakeSnmpClient(
        values={},
        walk_results={
            "1.3.6.1.4.1.161.19.3.1.4.1": _sm_subtree_rows(
                online_luids=["001"],
                degraded_luids=["002"],
                pre_existing_luids=["003", "004", "005"],
            ),
        },
    )
    settings = _settings_with_rollback_timeout(60)
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    call_log: list[str] = []

    def spy_migrate_subscriber(
        *,
        category: str,
        luid: str,
        target_frequency_mhz: float,
        client: Any,
    ) -> dict[str, Any]:
        call_log.append(f"{category}:{luid}")
        return {"status": "OK"}

    monkeypatch.setattr(migrate_mod, "migrate_subscriber", spy_migrate_subscriber)
    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", lambda **kw: None)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "_wait_for_management_reachability", lambda **kw: True)
    monkeypatch.setattr(
        migrate_mod,
        "save_intervention_record",
        lambda *a, **kw: {
            "status": "OK",
            "intervention_id": "INT-test",
        },
    )

    valid_token = _mint_valid_token()
    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=5800.0,
        settings=settings,
    )

    # No PRE_EXISTING_OFFLINE entries in the migration call log.
    pre_existing_calls = [entry for entry in call_log if entry.startswith("PRE_EXISTING_OFFLINE:")]
    assert pre_existing_calls == [], (
        f"PRE_EXISTING_OFFLINE SMs MUST be excluded; got {pre_existing_calls!r}"
    )
    # 1 ONLINE_ACTIVE + 1 ACTIVE_DEGRADED = 2 subscriber migrations.
    assert call_log == ["ONLINE_ACTIVE:001", "ACTIVE_DEGRADED:002"]
    assert result["pre_existing_offline_excluded"] == 3


# ---------------------------------------------------------------------------
# Named test #4 — migrate_rolls_back_within_timeout_on_loss_of_management
# ---------------------------------------------------------------------------


def test_migrate_rolls_back_within_timeout_on_loss_of_management(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Watchdog fires on loss-of-management; intervention record reports rollback.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 3 requirement
    "Rollback Watchdog With Timeout": the ``threading.Timer``
    watchdog reverts the SET frame to the prior carrier frequency on
    loss-of-management AND emits an intervention record with
    ``rolled_back: True``.
    """
    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    canned = _FakeSnmpClient(
        values={},
        walk_results={
            "1.3.6.1.4.1.161.19.3.1.4.1": _sm_subtree_rows(
                online_luids=["001"],
                degraded_luids=[],
                pre_existing_luids=[],
            ),
        },
    )
    settings = _settings_with_rollback_timeout(0)
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
        raising=False,
    )
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    # Capture watchdog state so the test can fire it deterministically.
    watchdog_callbacks: dict[str, Any] = {}
    watchdog_event = threading.Event()

    def fake_start_watchdog(*, timeout_seconds: int, on_loss_of_management: Any) -> None:
        watchdog_callbacks["on_loss_of_management"] = on_loss_of_management

    def fake_cancel_watchdog(*args: Any, **kwargs: Any) -> None:
        watchdog_event.set()

    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", fake_start_watchdog)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", fake_cancel_watchdog)
    monkeypatch.setattr(
        migrate_mod,
        "migrate_subscriber",
        lambda **kw: {"status": "OK"},
    )

    # Capture the intervention record emitted on completion.
    captured_records: list[dict[str, Any]] = []

    def fake_save(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured_records.append({"args": args, "kwargs": kwargs})
        return {"status": "OK", "intervention_id": "INT-test"}

    monkeypatch.setattr(migrate_mod, "save_intervention_record", fake_save)

    valid_token = _mint_valid_token()

    # Start the migration in a thread so we can fire the rollback callback
    # without blocking the test on the real Timer.
    result_box: dict[str, Any] = {}

    def _run() -> None:
        result_box["result"] = migrate_mod.fetch_migrate(
            driver=driver,
            device_id="ap-7400-01",
            approval_token=valid_token,
            target_frequency_mhz=5800.0,
            settings=settings,
        )

    thread = threading.Thread(target=_run)
    thread.start()

    # Give the migration a moment to start the watchdog.
    time.sleep(0.05)

    # Fire the watchdog synchronously (simulates loss-of-management).
    on_loss = watchdog_callbacks["on_loss_of_management"]
    on_loss()

    thread.join(timeout=2.0)
    assert not thread.is_alive(), "migrate thread did not finish after watchdog fired"

    # ``save_intervention_record`` was called exactly once.
    assert len(captured_records) == 1, (
        f"Expected exactly 1 save_intervention_record call; got {len(captured_records)}"
    )
    # signature is ``save_intervention_record(settings, payload)`` —
    # payload is the second positional arg.
    record = captured_records[0]["args"][1]
    assert record.get("rolled_back") is True, (
        f"intervention record MUST report rolled_back=True on loss-of-management; got {record!r}"
    )
    assert record.get("reason") == "loss_of_management"

    # The migration SET frame is the last ``_set_calls`` entry; the
    # watchdog's revert SET is appended AFTER it.
    assert len(canned._set_calls) >= 2, (
        f"Watchdog MUST issue a revert SET; got {canned._set_calls!r}"
    )
    assert canned._set_calls[-1].startswith("1.3.6.1.4.1.161.19.3.1.4.1.38.0="), (
        f"Revert SET MUST target the prior-carrier OID; got {canned._set_calls[-1]!r}"
    )


# ---------------------------------------------------------------------------
# Named test #5 — migrate_autonomous_call_raises_autonomous_mutation_rejected
# ---------------------------------------------------------------------------


def test_migrate_autonomous_call_raises_autonomous_mutation_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct call without a token raises the literal exception.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 3 requirement
    "Approval Token Contract": the verifier seam fires for ANY
    caller that bypasses the MCP wrapper — including direct
    library calls. The literal ``AutonomousMutationRejected``
    message is the contract.
    """
    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.drivers.snmp_pmp450i.migrate import fetch_migrate

    expected = "autonomous device mutation rejected: HITL approval token required"

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    canned = _FakeSnmpClient(values={})
    settings = _settings_with_rollback_timeout(60)
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    # Direct library call without any approval token.
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        fetch_migrate(
            driver=driver,
            device_id="ap-7400-01",
            approval_token=None,
            target_frequency_mhz=5800.0,
            settings=settings,
        )
    assert str(exc_info.value) == expected

    # Zero SET frames emitted.
    assert canned._set_calls == [], (
        f"Autonomous call MUST NOT emit SET frames; got {canned._set_calls!r}"
    )


# ---------------------------------------------------------------------------
# Named test #6 — migrate_emits_intervention_record_on_completion
# ---------------------------------------------------------------------------


def test_migrate_emits_intervention_record_on_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``save_intervention_record`` is called once per migration completion.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 3 requirement
    "Intervention Record Emission On Migration Completion": the
    tool MUST emit one ``POST_MIGRATION`` record per completion
    (rolled back or not). The test spies on
    :func:`save_intervention_record` and asserts the call shape.
    """
    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    canned = _FakeSnmpClient(
        values={},
        walk_results={
            "1.3.6.1.4.1.161.19.3.1.4.1": _sm_subtree_rows(
                online_luids=["001"],
                degraded_luids=["002"],
                pre_existing_luids=["003"],
            ),
        },
    )
    settings = _settings_with_rollback_timeout(60)
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    monkeypatch.setattr(
        migrate_mod,
        "migrate_subscriber",
        lambda **kw: {"status": "OK"},
    )
    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", lambda **kw: None)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "_wait_for_management_reachability", lambda **kw: True)

    captured_calls: list[dict[str, Any]] = []

    def fake_save(*args: Any, **kwargs: Any) -> dict[str, Any]:
        captured_calls.append({"args": args, "kwargs": kwargs})
        return {"status": "OK", "intervention_id": "INT-test-123"}

    monkeypatch.setattr(migrate_mod, "save_intervention_record", fake_save)

    valid_token = _mint_valid_token()
    migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=5800.0,
        settings=settings,
    )

    assert len(captured_calls) == 1, (
        f"save_intervention_record MUST be called exactly once; got {len(captured_calls)}"
    )
    # signature is ``save_intervention_record(settings, payload)`` —
    # payload is the second positional arg.
    record = captured_calls[0]["args"][1]
    assert record["stage"] == "POST_MIGRATION", (
        f"intervention record MUST carry stage=POST_MIGRATION; got {record!r}"
    )
    # ``target_frequency_mhz`` lands in ``network_equipment.carrier_frequency_mhz``
    # per the InterventionMemoryRecord schema (target_frequency_mhz is a
    # typed field on the NetworkEquipmentBlock, not on the top-level record).
    assert record["network_equipment"]["carrier_frequency_mhz"] == 5800.0, (
        f"intervention record MUST carry the carrier frequency; got {record!r}"
    )


# ---------------------------------------------------------------------------
# WU-3 follow-up — dry-run fallback when client lacks apply_oid.
#
# Production ``V2CClient`` (read-only ``SnmpClient`` Protocol contract)
# does NOT define ``apply_oid``. The unit suite passes because the
# ``_FakeSnmpClient`` defines it. Running against real hardware
# raises ``AttributeError`` AFTER the HITL gate clears. Per
# ``odd/tasks/pr44-followups.md`` WU-3: when the client lacks
# ``apply_oid``, ``fetch_migrate`` returns a typed dry-run result
# carrying the would-be SET pair, instead of crashing. We do NOT
# add ``apply_oid`` to ``V2CClient`` (write mutations stay out of
# scope).
# ---------------------------------------------------------------------------


class _V2CShapeStub:
    """V2CClient-shaped stub: ``get_oid`` + ``walk`` + ``close``. NO ``apply_oid``.

    Mirrors the production read-only ``SnmpClient`` Protocol — exactly
    the surface that ``V2CClient`` exposes. WU-3 dry-run fallback is
    gated on this stub LACKING ``apply_oid``.

    Issue #80 (PR #80 follow-up): the gate moved from ``apply_oid``
    to the ``WritableSnmpClient`` ``set`` verb. This stub
    deliberately exposes NEITHER — that is the contract seam. The
    production helper's ``hasattr(client, "set")`` check fires the
    dry-run fallback path against this stub.
    """

    def __init__(
        self,
        *,
        values: dict[str, str | int] | None = None,
        walk_results: dict[str, list[tuple[str, str | int]]] | None = None,
    ) -> None:
        self._values = dict(values or {})
        self._walk_results: dict[str, list[tuple[str, str | int]]] = walk_results or {}
        self.get_calls: list[str] = []
        self.walk_calls: list[str] = []
        self.close_calls: int = 0

    def get_oid(self, oid: str) -> str | int:
        self.get_calls.append(oid)
        if oid not in self._values:
            raise KeyError(oid)
        return self._values[oid]

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        self.walk_calls.append(base_oid)
        return list(self._walk_results.get(base_oid, []))

    def close(self) -> None:
        self.close_calls += 1

    # NOTE: NO ``apply_oid`` AND NO ``set`` — this is the contract
    # seam. The dry-run fallback path is gated on the absence of
    # the ``set`` verb (the ``WritableSnmpClient`` Protocol surface).


def _build_driver_with_stub(
    *,
    inventory: Inventory,
    registry: OidCatalogRegistry,
    stub: _V2CShapeStub,
    settings: Settings | None = None,
) -> Any:
    """Wire a driver whose factories return a V2C-shaped stub (no ``set``).

    Issue #80: BOTH ``client_factory`` and ``writable_client_factory``
    must point at the same V2C-shaped stub so the production helper's
    factory swap reaches the fake on both paths. The stub lacks the
    ``set`` verb by design — the gate fires the dry-run fallback.
    """
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver

    driver = Pmp450iSnmpDriver(
        inventory=inventory,
        catalog_registry=registry,
        client_factory=lambda d: stub,
        writable_client_factory=lambda d: stub,
    )
    if settings is not None:
        driver._runtime_settings = settings
    return driver


# ---------------------------------------------------------------------------
# WU-3 Named test #1 — emulation_dry_run_when_client_lacks_apply_oid
# ---------------------------------------------------------------------------


def test_fetch_migrate_emulation_dry_run_when_client_lacks_apply_oid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V2C-shaped client (no ``apply_oid``) → typed dry-run result with ``would_set`` pair.

    Per ``odd/tasks/pr44-followups.md`` WU-3: when the SNMP client
    lacks ``apply_oid``, ``fetch_migrate`` returns a typed dry-run
    result carrying the would-be SET pair, instead of crashing
    with ``AttributeError``. The dry-run path does NOT change the
    per-SM migration loop; it only short-circuits the AP
    carrier-change step.
    """
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    target_freq_mhz = 5800.0
    migration_freq_oid = "1.3.6.1.4.1.161.19.3.1.4.1.38.0"

    stub = _V2CShapeStub(
        values={
            # ``prior_carrier`` capture path — populate so ``get_oid`` returns the value.
            migration_freq_oid: 5750.0,
        },
        walk_results={
            "1.3.6.1.4.1.161.19.3.1.4.1": _sm_subtree_rows(
                online_luids=["001", "002"],
                degraded_luids=["003"],
                pre_existing_luids=["004"],
            ),
        },
    )
    settings = _settings_with_rollback_timeout(60)
    driver = _build_driver_with_stub(inventory=inv, registry=registry, stub=stub, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    # The dry-run path does NOT arm a watchdog / poll reachability, but
    # stub them defensively so the test cannot accidentally depend on
    # them being called.
    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", lambda **kw: None)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    valid_token = _mint_valid_token()
    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=target_freq_mhz,
        settings=settings,
    )

    # Dry-run typed result.
    assert result["dry_run"] is True, (
        f"V2C-shaped client MUST produce a dry-run result; got {result!r}"
    )
    # The single AP-side SET the tool WOULD have emitted — serialised
    # as a JSON array (Pydantic tuple→list).
    assert result["would_set"] == [[migration_freq_oid, target_freq_mhz]], (
        f"would_set MUST carry the carrier-frequency SET pair; got {result['would_set']!r}"
    )
    # No SET verb exists on the V2C-shaped stub — compile-time seam.
    assert not hasattr(stub, "apply_oid"), (
        "V2C-shaped stub MUST NOT define apply_oid — that is the contract seam"
    )
    # No rollback (no SET means no rollback window).
    assert result["rolled_back"] is False
    assert result["reason"] is None
    # Per-SM migration counts preserved.
    assert result["online_active_migrated"] == 2
    assert result["active_degraded_migrated"] == 1
    assert result["pre_existing_offline_excluded"] == 1
    # Device id is the inventory host.
    assert result["device_id"] == "192.0.2.10"
    # Target frequency echoed back.
    assert result["target_frequency_mhz"] == target_freq_mhz


# ---------------------------------------------------------------------------
# WU-3 Named test #2 — emulation_skips_watchdog_on_dry_run
# ---------------------------------------------------------------------------


def test_fetch_migrate_emulation_skips_watchdog_on_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run does NOT arm a rollback watchdog — nothing was SET.

    Per ``odd/tasks/pr44-followups.md`` WU-3: the dry-run path emits
    no SET frame, so there is no rollback window to manage. The
    ``_start_rollback_watchdog`` helper MUST NOT be called.
    """
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")

    stub = _V2CShapeStub(
        values={"1.3.6.1.4.1.161.19.3.1.4.1.38.0": 5750.0},
        walk_results={
            "1.3.6.1.4.1.161.19.3.1.4.1": _sm_subtree_rows(
                online_luids=["001"],
                degraded_luids=[],
                pre_existing_luids=[],
            ),
        },
    )
    settings = _settings_with_rollback_timeout(60)
    driver = _build_driver_with_stub(inventory=inv, registry=registry, stub=stub, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(migrate_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    # Spy on ``_start_rollback_watchdog`` so we can assert it was NEVER called.
    watchdog_calls: list[dict[str, Any]] = []

    def fake_start_watchdog(*, timeout_seconds: int, on_loss_of_management: Any) -> None:
        watchdog_calls.append({"timeout_seconds": timeout_seconds})

    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", fake_start_watchdog)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)

    valid_token = _mint_valid_token()
    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=5800.0,
        settings=settings,
    )

    # Dry-run; no watchdog armed.
    assert result["dry_run"] is True
    assert watchdog_calls == [], (
        f"Dry-run path MUST NOT arm a rollback watchdog; got {watchdog_calls!r}"
    )


# ---------------------------------------------------------------------------
# WU-3 Named test #3 — emulation_does_not_call_apply_oid_unavailable
# ---------------------------------------------------------------------------


def test_fetch_migrate_emulation_does_not_call_apply_oid_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dry-run does NOT call ``apply_oid`` — the gate is a ``hasattr`` check.

    Per ``odd/tasks/pr44-followups.md`` WU-3: the tool MUST
    short-circuit before any ``apply_oid`` call when the client
    lacks the method. The compile-time ``hasattr(client,
    'apply_oid')`` check is the contract seam — no
    ``AttributeError`` at runtime.
    """
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")

    stub = _V2CShapeStub(
        values={"1.3.6.1.4.1.161.19.3.1.4.1.38.0": 5750.0},
        walk_results={
            "1.3.6.1.4.1.161.19.3.1.4.1": _sm_subtree_rows(
                online_luids=["001"],
                degraded_luids=[],
                pre_existing_luids=[],
            ),
        },
    )
    settings = _settings_with_rollback_timeout(60)
    driver = _build_driver_with_stub(inventory=inv, registry=registry, stub=stub, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", lambda **kw: None)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    # Compile-time seam: ``hasattr`` must be the gate.
    assert not hasattr(stub, "apply_oid"), "V2C-shaped stub MUST lack apply_oid — the dry-run gate"

    valid_token = _mint_valid_token()
    # If the gate is missing, this call raises ``AttributeError``. A
    # clean return is the assertion.
    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=5800.0,
        settings=settings,
    )
    assert result["dry_run"] is True


# ---------------------------------------------------------------------------
# Issue #80 — real_set_still_works_via_set (regression guard).
#
# Renamed from the legacy ``test_fetch_migrate_real_set_still_works_via_apply_oid``
# after the production helper moved from the synthetic ``apply_oid``
# gate to the ``WritableSnmpClient`` ``set`` verb. The test still pins
# the real-SET path; only the verb and the value unit changed.
# ---------------------------------------------------------------------------


def test_fetch_migrate_real_set_still_works_via_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: ``_FakeSnmpClient`` with ``set`` keeps the real-SET path.

    Per issue #80 (PR #80 follow-up): the dry-run fallback MUST NOT
    regress the existing real-SET path. The production helper now
    emits the carrier-frequency SET via ``client.set(oid, value)``
    with the value converted to kHz (Cambium WHISP-APS-MIB native
    unit on ``radioFreqCarrier``). A test fake that exposes ``set``
    continues to flow through the unchanged wire path; the result
    carries ``dry_run=False, would_set=[]`` and the recorded SET
    frame carries the kHz value (``5800.0`` MHz → ``5_800_000`` kHz).
    """
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    target_freq_mhz = 5800.0
    target_freq_khz = int(round(target_freq_mhz * 1000))  # 5_800_000
    migration_freq_oid = "1.3.6.1.4.1.161.19.3.1.4.1.38.0"

    canned = _FakeSnmpClient(
        values={},
        walk_results={
            "1.3.6.1.4.1.161.19.3.1.4.1": _sm_subtree_rows(
                online_luids=["001"],
                degraded_luids=["002"],
                pre_existing_luids=["003"],
            ),
        },
    )
    settings = _settings_with_rollback_timeout(60)
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", lambda **kw: None)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "_wait_for_management_reachability", lambda **kw: True)
    monkeypatch.setattr(migrate_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    valid_token = _mint_valid_token()
    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=target_freq_mhz,
        settings=settings,
    )

    # Real SET path: NOT a dry-run; ``would_set`` is empty.
    assert result["dry_run"] is False, f"set-bearing client MUST NOT enter dry-run; got {result!r}"
    assert result["would_set"] == [], (
        f"Real-SET path MUST NOT populate would_set; got {result['would_set']!r}"
    )
    # Existing behaviour preserved: exactly one SET frame was emitted.
    assert len(canned._set_calls) == 1, (
        f"Real-SET path MUST emit exactly 1 SET frame; got {canned._set_calls!r}"
    )
    # Issue #80: SET value is in kHz (Cambium WHISP-APS-MIB native
    # unit on ``radioFreqCarrier``), NOT MHz. Production converts
    # via ``int(round(target_frequency_mhz * 1000))``.
    assert canned._set_calls[0] == f"{migration_freq_oid}={target_freq_khz}", (
        f"Real-SET path MUST emit the carrier-frequency SET in kHz; got {canned._set_calls[0]!r}"
    )


# ---------------------------------------------------------------------------
# Defensive coverage — exercise the public helpers directly so the
# per-module coverage targets (``migrate.py`` >= 90%) are met without
# weakening the production code.
# ---------------------------------------------------------------------------


def test_migrate_subscriber_default_logs_and_returns_ok() -> None:
    """The default ``migrate_subscriber`` returns ``{"status": "OK"}``.

    Defensive coverage for the production default — production
    slices replace this with a real SET frame, but the placeholder
    still ships so an un-monkeypatched caller has a working helper.
    """
    from nora.drivers.snmp_pmp450i.migrate import migrate_subscriber

    result = migrate_subscriber(
        category="ONLINE_ACTIVE",
        luid="001",
        target_frequency_mhz=5800.0,
        client=None,
    )
    assert result["status"] == "OK"
    assert result["category"] == "ONLINE_ACTIVE"
    assert result["luid"] == "001"


def test_rollback_watchdog_start_and_cancel_round_trip() -> None:
    """The default ``_start_rollback_watchdog`` + ``_cancel_rollback_watchdog`` round-trip.

    Defensive coverage for the watchdog seam — production uses
    ``threading.Timer``; the test exercises the real helpers with
    a short timeout so the timer can be cancelled before it fires.
    """
    import threading

    from nora.drivers.snmp_pmp450i.migrate import (
        _cancel_rollback_watchdog,
        _start_rollback_watchdog,
    )

    fired_flag = threading.Event()

    def _never_fire() -> None:
        fired_flag.set()

    timer = _start_rollback_watchdog(timeout_seconds=60, on_loss_of_management=_never_fire)
    assert isinstance(timer, threading.Timer)
    _cancel_rollback_watchdog(timer)
    _cancel_rollback_watchdog(None)  # idempotent
    assert not fired_flag.is_set(), (
        "Cancelled watchdog MUST NOT fire; the migration returned cleanly."
    )


# ---------------------------------------------------------------------------
# WU-A (feat/multi-community-band-reboot) — pre-flight community
# validation. The pre-flight runs BEFORE the HITL token gate so the
# operator is never asked to mint an approval token for a migration
# we already know will fail at the community-string level.
#
# Five scenarios from the feature doc:
#   1. AP reachable + all SMs reachable -> pass (no CommunityValidationFailed)
#   2. One SM unreachable -> DeviceUnreachable in report
#   3. One SM wrong community -> InvalidCommunity in report
#   4. SM not in inventory -> MISSING_INVENTORY_ENTRY
#   5. Mix of failures -> all three error_classes surfaced
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


class _RoutedFakeSnmpClient:
    """Fake client that routes sysDescr per host.

    The WU-A pre-flight opens one client per SM (and one for the AP).
    Each client must answer its own sysDescr GET — either with a
    canned body on the success path or by raising a typed exception
    on the failure path.

    Issue #80: this fake now also exposes the ``set`` verb (a
    no-op, like the legacy ``apply_oid``) so the migration's
    writable-factory path doesn't fall through to the real
    ``WritableV2CClient`` when these tests don't override the
    writable factory seam.
    """

    def __init__(
        self,
        *,
        per_host_sysdescr: dict[str, str] | None = None,
        per_host_raises: dict[str, type[BaseException]] | None = None,
        default_sysdescr: str = "Cambium PMP 450i AP 15.2.1",
    ) -> None:
        self._per_host_sysdescr = per_host_sysdescr or {}
        self._per_host_raises = per_host_raises or {}
        self._default_sysdescr = default_sysdescr
        self.get_calls: list[tuple[str, str]] = []  # (host, oid)

    def get_oid(self, oid: str) -> str | int:
        # The pre-flight calls get_oid with sysDescr; remember the
        # host the call came from by inspecting ``self._current_host``.
        host = getattr(self, "_current_host", "?")
        self.get_calls.append((host, oid))
        if host in self._per_host_raises:
            raise self._per_host_raises[host](f"simulated failure for {host}")
        if host in self._per_host_sysdescr:
            return self._per_host_sysdescr[host]
        return self._default_sysdescr

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        return []

    def apply_oid(self, oid: str, value: str | int) -> None:
        return None

    def set(self, oid: str, value: str | int) -> None:
        # Issue #80: no-op stub for the ``WritableSnmpClient``
        # Protocol verb. The migration path's gate fires the
        # dry-run fallback if this verb is missing, which is NOT
        # what the routing tests want — they exercise the full
        # pre-flight + migration path and expect the migration to
        # complete. Defining ``set`` as a no-op lets the gate pass
        # without changing the routing tests' observable behavior.
        return None

    def close(self) -> None:
        return None


def _routing_client_factory(routed: _RoutedFakeSnmpClient) -> Callable[[Any], Any]:
    """Build a client factory that tags the fake with the device's host."""

    def _factory(device: Any) -> Any:
        # The fake tracks the current host via a transient attribute.
        routed._current_host = str(getattr(device, "host", "?"))
        return routed

    return _factory


def test_preflight_passes_when_ap_and_all_sms_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WU-A scenario 1: AP reachable + all SMs reachable -> pre-flight passes.

    The migration proceeds; ``CommunityValidationFailed`` is NOT
    raised. The HITL gate then fires (token gate is preserved).
    """
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    inv = _build_inventory_with_sms(tmp_path, sm_luids=("001", "002"))
    registry = _build_catalog(firmware="15.2.1")
    routed = _RoutedFakeSnmpClient(
        per_host_sysdescr={
            "192.0.2.10": "Cambium PMP 450i AP 15.2.1",
            "192.0.2.20": "Cambium PMP 450i SM 001 15.2.1",
            "192.0.2.21": "Cambium PMP 450i SM 002 15.2.1",
        },
    )
    settings = _settings_with_rollback_timeout(60, preflight_community_validation=True)
    driver = _build_driver(
        inventory=inv,
        registry=registry,
        canned=routed,
        settings=settings,
    )
    # Override client_factory so the fake tracks the current host.
    monkeypatch.setattr(driver, "_client_factory", _routing_client_factory(routed))

    # SM-table subtree reports both LUIDs as ONLINE_ACTIVE.
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
    # the full SET path.
    monkeypatch.setattr(migrate_mod, "_start_rollback_watchdog", lambda **kw: None)
    monkeypatch.setattr(migrate_mod, "_cancel_rollback_watchdog", lambda *a, **kw: None)
    monkeypatch.setattr(migrate_mod, "_wait_for_management_reachability", lambda **kw: True)
    monkeypatch.setattr(migrate_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    valid_token = _mint_valid_token()
    result = migrate_mod.fetch_migrate(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        target_frequency_mhz=5800.0,
        settings=settings,
    )
    # No CommunityValidationFailed was raised; the pre-flight succeeded.
    assert "rolled_back" in result, f"Expected a MigrationResult-shaped dict; got {result!r}"
    assert result["rolled_back"] is False


def test_preflight_raises_when_one_sm_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WU-A scenario 2: one SM unreachable -> DeviceUnreachable in report."""
    from nora.drivers.exceptions import CommunityValidationFailed, SnmpTimeoutError
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    inv = _build_inventory_with_sms(tmp_path, sm_luids=("001", "002"))
    registry = _build_catalog(firmware="15.2.1")
    routed = _RoutedFakeSnmpClient(
        per_host_sysdescr={
            "192.0.2.21": "Cambium PMP 450i SM 002 15.2.1",
        },
        per_host_raises={
            "192.0.2.20": SnmpTimeoutError,
        },
    )
    settings = _settings_with_rollback_timeout(60, preflight_community_validation=True)
    driver = _build_driver(inventory=inv, registry=registry, canned=routed, settings=settings)
    monkeypatch.setattr(driver, "_client_factory", _routing_client_factory(routed))
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        migrate_mod,
        "fetch_sm_table",
        lambda **kw: _fake_sm_summary(online_luids=("001", "002"), degraded_luids=()),
    )

    with pytest.raises(CommunityValidationFailed) as exc_info:
        migrate_mod.fetch_migrate(
            driver=driver,
            device_id="ap-7400-01",
            approval_token=_mint_valid_token(),
            target_frequency_mhz=5800.0,
            settings=settings,
        )

    report = exc_info.value.report
    assert report.ap_reachable is True
    failed = [r for r in report.sm_results if r.luid == "001"]
    assert len(failed) == 1
    assert failed[0].error_class == "DeviceUnreachable"
    assert failed[0].host == "192.0.2.20"


def test_preflight_raises_when_one_sm_wrong_community(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WU-A scenario 3: one SM wrong community -> InvalidCommunity in report."""
    from nora.drivers.exceptions import CommunityValidationFailed, NetworkUnreachableError
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    inv = _build_inventory_with_sms(tmp_path, sm_luids=("001", "002"))
    registry = _build_catalog(firmware="15.2.1")
    routed = _RoutedFakeSnmpClient(
        per_host_raises={
            "192.0.2.20": NetworkUnreachableError,
        },
        per_host_sysdescr={
            "192.0.2.21": "Cambium PMP 450i SM 002 15.2.1",
        },
    )
    settings = _settings_with_rollback_timeout(60, preflight_community_validation=True)
    driver = _build_driver(inventory=inv, registry=registry, canned=routed, settings=settings)
    monkeypatch.setattr(driver, "_client_factory", _routing_client_factory(routed))
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        migrate_mod,
        "fetch_sm_table",
        lambda **kw: _fake_sm_summary(online_luids=("001", "002"), degraded_luids=()),
    )

    with pytest.raises(CommunityValidationFailed) as exc_info:
        migrate_mod.fetch_migrate(
            driver=driver,
            device_id="ap-7400-01",
            approval_token=_mint_valid_token(),
            target_frequency_mhz=5800.0,
            settings=settings,
        )

    report = exc_info.value.report
    failed = [r for r in report.sm_results if r.luid == "001"]
    assert len(failed) == 1
    assert failed[0].error_class == "InvalidCommunity"
    assert failed[0].host == "192.0.2.20"


def test_preflight_raises_when_sm_not_in_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WU-A scenario 4: SM missing from inventory -> MISSING_INVENTORY_ENTRY."""
    from nora.drivers.exceptions import CommunityValidationFailed
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    # Only SM 001 in inventory; SM 002 is absent.
    inv = _build_inventory_with_sms(tmp_path, sm_luids=("001",))
    registry = _build_catalog(firmware="15.2.1")
    routed = _RoutedFakeSnmpClient(
        per_host_sysdescr={
            "192.0.2.20": "Cambium PMP 450i SM 001 15.2.1",
        },
    )
    settings = _settings_with_rollback_timeout(60, preflight_community_validation=True)
    driver = _build_driver(inventory=inv, registry=registry, canned=routed, settings=settings)
    monkeypatch.setattr(driver, "_client_factory", _routing_client_factory(routed))
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        migrate_mod,
        "fetch_sm_table",
        lambda **kw: _fake_sm_summary(online_luids=("001", "002"), degraded_luids=()),
    )

    with pytest.raises(CommunityValidationFailed) as exc_info:
        migrate_mod.fetch_migrate(
            driver=driver,
            device_id="ap-7400-01",
            approval_token=_mint_valid_token(),
            target_frequency_mhz=5800.0,
            settings=settings,
        )

    report = exc_info.value.report
    assert "002" in report.missing_inventory_luids
    failed = [r for r in report.sm_results if r.luid == "002"]
    assert len(failed) == 1
    assert failed[0].error_class == "MISSING_INVENTORY_ENTRY"


def test_preflight_raises_when_ap_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WU-A edge case: AP unreachable -> pre-flight fails fast, no SM probes.

    The orchestrator must see ``ap_reachable=False`` and the absence
    of any SM probe so the operator can investigate the AP-side
    problem first.
    """
    from nora.drivers.exceptions import CommunityValidationFailed, NetworkUnreachableError
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    inv = _build_inventory_with_sms(tmp_path, sm_luids=("001",))
    registry = _build_catalog(firmware="15.2.1")
    routed = _RoutedFakeSnmpClient(
        per_host_raises={
            "192.0.2.10": NetworkUnreachableError,
        },
    )
    settings = _settings_with_rollback_timeout(60, preflight_community_validation=True)
    driver = _build_driver(inventory=inv, registry=registry, canned=routed, settings=settings)
    monkeypatch.setattr(driver, "_client_factory", _routing_client_factory(routed))
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        migrate_mod,
        "fetch_sm_table",
        lambda **kw: _fake_sm_summary(online_luids=("001",), degraded_luids=()),
    )

    with pytest.raises(CommunityValidationFailed) as exc_info:
        migrate_mod.fetch_migrate(
            driver=driver,
            device_id="ap-7400-01",
            approval_token=_mint_valid_token(),
            target_frequency_mhz=5800.0,
            settings=settings,
        )

    report = exc_info.value.report
    assert report.ap_reachable is False
    assert report.sm_results == [], (
        "AP unreachable: SM probes MUST be skipped to avoid flooding a broken network"
    )


def test_preflight_does_not_consume_hitl_token_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WU-A decision: pre-flight fails BEFORE the HITL gate.

    The operator does not pay for an approval token on a known-bad
    migration.
    """
    from nora.drivers.exceptions import CommunityValidationFailed, NetworkUnreachableError
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod
    from nora.hitl import tokens as hitl_tokens_mod

    inv = _build_inventory_with_sms(tmp_path, sm_luids=("001",))
    registry = _build_catalog(firmware="15.2.1")
    routed = _RoutedFakeSnmpClient(
        per_host_raises={"192.0.2.20": NetworkUnreachableError},
    )
    settings = _settings_with_rollback_timeout(60, preflight_community_validation=True)
    driver = _build_driver(inventory=inv, registry=registry, canned=routed, settings=settings)
    monkeypatch.setattr(driver, "_client_factory", _routing_client_factory(routed))
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        migrate_mod,
        "fetch_sm_table",
        lambda **kw: _fake_sm_summary(online_luids=("001",), degraded_luids=()),
    )

    verify_calls: list[Any] = []

    def spy_verify(token: Any, **kw: Any) -> None:
        verify_calls.append(token)
        # The token would be valid here; the gate fires if reached.
        return None

    monkeypatch.setattr(hitl_tokens_mod, "verify_approval_token", spy_verify)
    monkeypatch.setattr(migrate_mod, "verify_approval_token", spy_verify)

    with pytest.raises(CommunityValidationFailed):
        migrate_mod.fetch_migrate(
            driver=driver,
            device_id="ap-7400-01",
            approval_token=_mint_valid_token(),
            target_frequency_mhz=5800.0,
            settings=settings,
        )

    # Crucial: the HITL verifier was NOT called because the pre-flight
    # failed first.
    assert verify_calls == [], (
        f"HITL token verifier MUST NOT run when pre-flight fails; got {verify_calls!r}"
    )


def _fake_sm_summary(*, online_luids: tuple[str, ...], degraded_luids: tuple[str, ...]) -> Any:
    """Build a minimal ``SubscriberSummary``-shaped namespace for pre-flight tests.

    The WU-A pre-flight only reads ``.online_active`` and
    ``.active_degraded``; we model the rest as empty to keep the
    fake hermetic.
    """
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
        fetched_at="2026-09-18T00:00:00+00:00",
    )


__all__ = [
    "test_migrate_requires_hitl_approval_token",
    "test_migrate_make_before_break_migrates_online_active_first",
    "test_migrate_excludes_pre_existing_offline_subscribers",
    "test_migrate_rolls_back_within_timeout_on_loss_of_management",
    "test_migrate_autonomous_call_raises_autonomous_mutation_rejected",
    "test_migrate_emits_intervention_record_on_completion",
    "test_fetch_migrate_emulation_dry_run_when_client_lacks_apply_oid",
    "test_fetch_migrate_emulation_skips_watchdog_on_dry_run",
    "test_fetch_migrate_emulation_does_not_call_apply_oid_unavailable",
    "test_fetch_migrate_real_set_still_works_via_set",
    "test_migrate_subscriber_default_logs_and_returns_ok",
    "test_rollback_watchdog_start_and_cancel_round_trip",
    # WU-A (feat/multi-community-band-reboot) — pre-flight community
    # validation scenarios. All six tests pin the WU-A contract.
    "test_preflight_passes_when_ap_and_all_sms_reachable",
    "test_preflight_raises_when_one_sm_unreachable",
    "test_preflight_raises_when_one_sm_wrong_community",
    "test_preflight_raises_when_sm_not_in_inventory",
    "test_preflight_raises_when_ap_unreachable",
    "test_preflight_does_not_consume_hitl_token_on_failure",
]
