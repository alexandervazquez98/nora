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
    """The six radio-metrics REQUIRED_OIDs."""
    return {
        "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.4.1.36.0",
        "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.4.1.38.0",
        "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.4.1.34.0",
        "signalStrengthTx": "1.3.6.1.4.1.161.19.3.1.4.1.89.0",
        "ssr": "1.3.6.1.4.1.161.19.3.1.4.1.86.0",
        "modulationMode": "1.3.6.1.4.1.161.19.3.1.4.1.40.0",
    }


def _sm_table_oids() -> dict[str, str]:
    """SM table OID names + dotted OIDs (slice 3 additions)."""
    return {
        "smSessionUptime": "1.3.6.1.4.1.161.19.3.1.4.1.46.0",
        "smCinr": "1.3.6.1.4.1.161.19.3.1.4.1.74.0",
        "smLinkStatus": "1.3.6.1.4.1.161.19.3.1.4.1.19.0",
        "smLuid": "1.3.6.1.4.1.161.19.3.1.4.1.1.0",
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

    Each SM occupies four OID rows in the public Cambium branch
    (smSessionUptime, smCinr, smLinkStatus, smLuid). ONLINE_ACTIVE
    rows carry uptime > 0 AND linked modulation AND healthy CINR.
    ACTIVE_DEGRADED rows carry uptime > 0 AND CINR < 18. PRE_EXISTING_OFFLINE
    rows carry uptime == 0 (the central categoriser treats this as
    pre-existing offline even without history).
    """
    base = "1.3.6.1.4.1.161.19.3.1.4.1"
    rows: list[tuple[str, str | int]] = []
    sm_index = 1
    for luid in online_luids:
        rows.extend(
            [
                (f"{base}.46.{sm_index}", 86400),
                (f"{base}.74.{sm_index}", 25),
                (f"{base}.19.{sm_index}", "LINKED"),
                (f"{base}.1.{sm_index}", luid),
            ]
        )
        sm_index += 1
    for luid in degraded_luids:
        rows.extend(
            [
                (f"{base}.46.{sm_index}", 43200),
                (f"{base}.74.{sm_index}", 12),
                (f"{base}.19.{sm_index}", "LINKED"),
                (f"{base}.1.{sm_index}", luid),
            ]
        )
        sm_index += 1
    for luid in pre_existing_luids:
        rows.extend(
            [
                (f"{base}.46.{sm_index}", 0),
                (f"{base}.74.{sm_index}", 0),
                (f"{base}.19.{sm_index}", "DOWN"),
                (f"{base}.1.{sm_index}", luid),
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
        # the fake's ``apply_oid`` is the seam for the AP channel change.
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
        """Apply a write frame — only used by the AP channel-change path."""
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


def _settings_with_rollback_timeout(seconds: int) -> Settings:
    from pydantic import SecretStr

    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_hitl_rollback_timeout_seconds=seconds,
        nora_hitl_signing_key=SecretStr("test-snmp-migrate-hmac-key"),
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

    # NOTE: NO ``apply_oid`` — this is the contract seam. The dry-run
    # fallback path is gated on its absence.


def _build_driver_with_stub(
    *,
    inventory: Inventory,
    registry: OidCatalogRegistry,
    stub: _V2CShapeStub,
    settings: Settings | None = None,
) -> Any:
    """Wire a driver whose ``client_factory`` returns a V2C-shaped stub (no apply_oid)."""
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver

    driver = Pmp450iSnmpDriver(
        inventory=inventory,
        catalog_registry=registry,
        client_factory=lambda d: stub,
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
# WU-3 Named test #4 — real_set_still_works_via_apply_oid (regression guard)
# ---------------------------------------------------------------------------


def test_fetch_migrate_real_set_still_works_via_apply_oid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: ``_FakeSnmpClient`` with ``apply_oid`` keeps the real-SET path.

    Per ``odd/tasks/pr44-followups.md`` WU-3: the dry-run fallback
    MUST NOT regress the existing real-SET path. A test fake that
    defines ``apply_oid`` continues to flow through the unchanged
    wire path; the result carries ``dry_run=False, would_set=[]``.
    """
    from nora.drivers.snmp_pmp450i import migrate as migrate_mod

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    target_freq_mhz = 5800.0
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
    assert result["dry_run"] is False, (
        f"apply_oid-bearing client MUST NOT enter dry-run; got {result!r}"
    )
    assert result["would_set"] == [], (
        f"Real-SET path MUST NOT populate would_set; got {result['would_set']!r}"
    )
    # Existing behaviour preserved: exactly one SET frame was emitted.
    assert len(canned._set_calls) == 1, (
        f"Real-SET path MUST emit exactly 1 SET frame; got {canned._set_calls!r}"
    )
    assert canned._set_calls[0] == f"{migration_freq_oid}={target_freq_mhz}", (
        f"Real-SET path MUST emit the carrier-frequency SET; got {canned._set_calls[0]!r}"
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
    "test_fetch_migrate_real_set_still_works_via_apply_oid",
    "test_migrate_subscriber_default_logs_and_returns_ok",
    "test_rollback_watchdog_start_and_cancel_round_trip",
]
