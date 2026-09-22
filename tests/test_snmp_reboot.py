"""Tests for the HITL-gated reboot tool — WU-C (feat/multi-community-band-reboot).

These tests pin the public contract for the WU-C ``snmp_reboot_radio``
MCP tool:

* HITL gate — missing or invalid approval tokens raise
  :class:`AutonomousMutationRejected` with the **literal** message
  ``"autonomous device mutation rejected: HITL approval token
  required"``. No wire frame is emitted.
* Firmware vote — the radio's ``rebootIfRequired`` OID is the
  authoritative signal; the tool returns ``rebooted=False`` when
  the vote says no (without emitting any SET frame).
* Real-SET path — when the firmware says reboot, the tool emits
  one SET on ``reboot`` (``1.3.6.1.4.1.161.19.3.3.3.2.0``,
  ``whispBoxControls 2``) with value ``fullReboot(2)`` (450i
  default).
* Dry-run fallback — when the SNMP client lacks ``apply_oid``
  (production ``V2CClient`` read-only Protocol contract), the tool
  returns ``dry_run=True, would_set=[(reboot_oid, 2)]`` without
  raising ``AttributeError``.
* Fail-closed on vote-unreadable — when ``rebootIfRequired`` cannot
  be read (wire failure), the helper default-fires the SET
  (``reason='vote_unreadable_fail_closed'``).
* Intervention record emission — the tool emits exactly one
  ``POST_REBOOT`` intervention record per completion (rebooted /
  skipped / dry-run).

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real
IPs, hostnames, serials, or credentials.
"""

from __future__ import annotations

import json
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
    """Hermetic inventory with one PMP 450i AP."""
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


def _reboot_oid_dict() -> dict[str, str]:
    """Reboot OID names + dotted OIDs (catalog v2 — see commit f85f2ae)."""
    return {
        "reboot": "1.3.6.1.4.1.161.19.3.3.3.2.0",
        "rebootIfRequired": "1.3.6.1.4.1.161.19.3.3.3.4.0",
    }


def _build_catalog(
    firmware: str = "15.2.1",
    *,
    include_reboot_oids: bool = True,
) -> OidCatalogRegistry:
    """Catalog registry carrying the radio seed + (optional) reboot OIDs."""
    oids: dict[str, str] = {}
    oids.update(_reboot_oid_dict()) if include_reboot_oids else None
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
    """Fake client — returns canned values keyed by dotted OID.

    Issue #80 (PR #80 follow-up): the production helper now
    reaches the writable factory seam AND the ``set`` verb
    (the ``WritableSnmpClient`` Protocol contract). The fake
    exposes BOTH ``apply_oid`` (kept for legacy assertions) AND
    ``set`` (the verb the production code actually calls). Both
    methods append to the same ``_set_calls`` list so existing
    assertions stay green.
    """

    def __init__(
        self,
        values: dict[str, str | int],
        *,
        set_calls: list[str] | None = None,
    ) -> None:
        self._values = dict(values)
        self.get_calls: list[str] = []
        self._set_calls: list[str] = set_calls if set_calls is not None else []

    def get_oid(self, oid: str) -> str | int:
        self.get_calls.append(oid)
        if oid not in self._values:
            raise KeyError(oid)
        return self._values[oid]

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        return []

    def apply_oid(self, oid: str, value: str | int) -> None:
        """Legacy seam — kept so old assertions still record frames.

        Production code now reaches ``set`` instead; both methods
        append to ``_set_calls``.
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


class _ReadOnlyClient:
    """Fake client WITHOUT apply_oid — production V2CClient shape."""

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
    canned: Any,
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
        # replaced ``_client_factory`` for the reboot SET path)
        # reaches the fake instead of the default
        # ``WritableV2CClient``. Without this, the legacy tests
        # would attempt a real-wire SET against ``192.0.2.10``
        # and time out after 5.0s.
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
        signing_key=SecretStr("test-snmp-reboot-hmac-key"),
    )
    return json.dumps(token_obj.model_dump(mode="json"))


def _settings_with_signing_key() -> Settings:
    from pydantic import SecretStr

    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_hitl_signing_key=SecretStr("test-snmp-reboot-hmac-key"),
    )


# ---------------------------------------------------------------------------
# Named test #1 — reboot_requires_hitl_approval_token
# ---------------------------------------------------------------------------


def test_reboot_requires_hitl_approval_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``fetch_reboot`` raises ``AutonomousMutationRejected`` on missing/invalid tokens."""
    from nora.drivers.exceptions import AutonomousMutationRejected
    from nora.drivers.snmp_pmp450i import reboot as reboot_mod

    expected = "autonomous device mutation rejected: HITL approval token required"

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    canned = _FakeSnmpClient(
        values={
            "1.3.6.1.4.1.161.19.3.3.3.4.0": 1,
        },
    )
    settings = _settings_with_signing_key()
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    monkeypatch.setattr(reboot_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    # Path 1: missing token (None).
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        reboot_mod.fetch_reboot(
            driver=driver,
            device_id="ap-7400-01",
            approval_token=None,
            settings=settings,
        )
    assert str(exc_info.value) == expected

    # Path 2: invalid token (empty string).
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        reboot_mod.fetch_reboot(
            driver=driver,
            device_id="ap-7400-01",
            approval_token="",
            settings=settings,
        )
    assert str(exc_info.value) == expected

    # Path 3: invalid token (non-JSON garbage).
    with pytest.raises(AutonomousMutationRejected) as exc_info:
        reboot_mod.fetch_reboot(
            driver=driver,
            device_id="ap-7400-01",
            approval_token="expired-or-bogus",
            settings=settings,
        )
    assert str(exc_info.value) == expected

    # Zero SET frames emitted.
    assert canned._set_calls == [], (
        f"reboot MUST NOT emit SET frames when the approval token is rejected; "
        f"got {canned._set_calls!r}"
    )


# ---------------------------------------------------------------------------
# Named test #2 — reboot_skips_when_firmware_says_not_required
# ---------------------------------------------------------------------------


def test_reboot_skips_when_firmware_says_not_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Firmware vote says ``rebootNotRequired`` -> no SET emitted."""
    from nora.drivers.snmp_pmp450i import reboot as reboot_mod

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    canned = _FakeSnmpClient(
        values={
            "1.3.6.1.4.1.161.19.3.3.3.4.0": 0,  # rebootNotRequired
        },
    )
    settings = _settings_with_signing_key()
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    monkeypatch.setattr(reboot_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    valid_token = _mint_valid_token()
    result = reboot_mod.fetch_reboot(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        settings=settings,
    )

    assert result["rebooted"] is False
    assert result["reason"] == "not_required_by_firmware"
    assert result["firmware_vote"] == 0
    assert canned._set_calls == [], (
        f"Firmware said not_required; MUST NOT emit SET; got {canned._set_calls!r}"
    )


# ---------------------------------------------------------------------------
# Named test #3 — reboot_real_set_emits_one_set_frame
# ---------------------------------------------------------------------------


def test_reboot_real_set_emits_one_set_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Firmware vote says ``rebootRequired`` -> exactly one SET on ``reboot``."""
    from nora.drivers.snmp_pmp450i import reboot as reboot_mod

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    canned = _FakeSnmpClient(
        values={
            "1.3.6.1.4.1.161.19.3.3.3.4.0": 1,  # rebootRequired
        },
    )
    settings = _settings_with_signing_key()
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    monkeypatch.setattr(reboot_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    valid_token = _mint_valid_token()
    result = reboot_mod.fetch_reboot(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        settings=settings,
    )

    assert result["rebooted"] is True
    assert result["reason"] == "reboot_required_by_firmware"
    assert result["firmware_vote"] == 1
    assert result["dry_run"] is False
    # Exactly one SET frame, fullReboot(2) for 450i.
    assert canned._set_calls == ["1.3.6.1.4.1.161.19.3.3.3.2.0=2"], (
        f"Expected exactly one fullReboot SET on reboot OID; got {canned._set_calls!r}"
    )
    # 450i default recovery hint.
    assert result["expected_recovery_seconds"] == 120
    # Tier-2 invariant.
    assert result["hitl_required"] is True


# ---------------------------------------------------------------------------
# Named test #4 — reboot_dry_run_when_client_lacks_apply_oid
# ---------------------------------------------------------------------------


def test_reboot_dry_run_when_client_lacks_apply_oid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Production V2CClient (no apply_oid) -> dry-run fallback.

    Same pattern as ``MigrationResult``: when the client lacks the
    write verb, the helper returns a typed dry-run result so the
    operator sees what WOULD have happened, instead of raising
    ``AttributeError``.
    """
    from nora.drivers.snmp_pmp450i import reboot as reboot_mod

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    canned = _ReadOnlyClient(
        values={
            "1.3.6.1.4.1.161.19.3.3.3.4.0": 1,  # rebootRequired
        },
    )
    settings = _settings_with_signing_key()
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    monkeypatch.setattr(reboot_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    valid_token = _mint_valid_token()
    result = reboot_mod.fetch_reboot(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        settings=settings,
    )

    assert result["rebooted"] is True
    assert result["dry_run"] is True
    # Pydantic serialises tuples as lists via ``model_dump(mode="json")``.
    assert result["would_set"] == [["1.3.6.1.4.1.161.19.3.3.3.2.0", 2]], (
        f"Dry-run path MUST populate would_set; got {result['would_set']!r}"
    )
    assert result["set_calls"] == []


# ---------------------------------------------------------------------------
# Named test #5 — reboot_fail_closed_when_vote_unreadable
# ---------------------------------------------------------------------------


def test_reboot_fail_closed_when_vote_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Wire failure on ``rebootIfRequired`` -> default-fire SET (fail-closed)."""
    from nora.drivers.snmp_pmp450i import reboot as reboot_mod

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    # No values dict -> get_oid raises KeyError -> vote_unreadable.
    canned = _FakeSnmpClient(values={})
    settings = _settings_with_signing_key()
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    monkeypatch.setattr(reboot_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    valid_token = _mint_valid_token()
    result = reboot_mod.fetch_reboot(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        settings=settings,
    )

    # Fail-closed: the SET fires even when the vote is unreadable.
    assert result["rebooted"] is True
    assert result["reason"] == "vote_unreadable_fail_closed"
    assert result["firmware_vote"] is None
    assert canned._set_calls == ["1.3.6.1.4.1.161.19.3.3.3.2.0=2"]


# ---------------------------------------------------------------------------
# Named test #6 — reboot_missing_oid_raises_lookup_error
# ---------------------------------------------------------------------------


def test_reboot_missing_oid_raises_lookup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catalog without ``reboot`` OID -> ``LookupError`` BEFORE the SET fires."""
    from nora.drivers.snmp_pmp450i import reboot as reboot_mod

    inv = _build_inventory(tmp_path)
    # Build a catalog MISSING the reboot OIDs.
    catalog = OidCatalog(
        vendor="cambium",
        model="pmp450i",
        firmware="15.2.1",
        oids={},  # no reboot OIDs
    )
    registry = OidCatalogRegistry(
        _catalogs_path=Path("."),
        _catalogs={("cambium", "pmp450i", "15.2.1"): catalog},
    )
    canned = _FakeSnmpClient(values={})
    settings = _settings_with_signing_key()
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    monkeypatch.setattr(reboot_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    valid_token = _mint_valid_token()
    with pytest.raises(LookupError) as exc_info:
        reboot_mod.fetch_reboot(
            driver=driver,
            device_id="ap-7400-01",
            approval_token=valid_token,
            settings=settings,
        )
    # Either OID name could trigger the LookupError depending on the
    # catalog lookup order; the helper checks `reboot` first.
    assert "reboot" in str(exc_info.value).lower()
    assert canned._set_calls == [], (
        f"MUST NOT emit SET when the catalog is missing the reboot OID; got {canned._set_calls!r}"
    )


# ---------------------------------------------------------------------------
# Named test #7 — reboot_emits_post_reboot_intervention_record
# ---------------------------------------------------------------------------


def test_reboot_emits_post_reboot_intervention_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every reboot path emits exactly one POST_REBOOT record.

    Mirrors the slice-4 contract for ``MigrationResult`` /
    ``POST_MIGRATION``. The tool body uses the existing
    ``save_intervention_record`` library function so the
    record-writing rules are uniform across tools.
    """
    from nora.drivers.snmp_pmp450i import reboot as reboot_mod

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    canned = _FakeSnmpClient(
        values={
            "1.3.6.1.4.1.161.19.3.3.3.4.0": 1,
        },
    )
    settings = _settings_with_signing_key()
    driver = _build_driver(inventory=inv, registry=registry, canned=canned, settings=settings)

    record_calls: list[dict[str, Any]] = []

    def spy_save(settings: Any, payload: dict[str, Any]) -> dict[str, Any]:
        record_calls.append(payload)
        return {"status": "OK", "intervention_id": "INT-REBOOT-test"}

    monkeypatch.setattr(reboot_mod, "save_intervention_record", spy_save)

    valid_token = _mint_valid_token()
    reboot_mod.fetch_reboot(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        settings=settings,
    )

    assert len(record_calls) == 1, (
        f"Expected exactly one POST_REBOOT record; got {len(record_calls)}"
    )
    payload = record_calls[0]
    assert payload["stage"] == "POST_REBOOT"
    assert payload["target_ip"] == "192.0.2.10"
    assert payload["status"] == "COMPLETED"
    assert "RF reboot of" in payload["record_name"]
    assert payload["rebooted"] is True
