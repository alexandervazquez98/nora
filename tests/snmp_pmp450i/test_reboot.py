"""Tests for issue #80 — wire-path regression tests for ``fetch_reboot``.

These tests pin the Tier-1 contract for ``snmp_reboot_radio``:

* When an operator authorizes a reboot with a valid HITL token, the
  helper MUST emit a real SET frame on the ``reboot`` OID
  (``1.3.6.1.4.1.161.19.3.3.3.2.0``, Cambium WHISP-BOX-MIBV2-MIB
  ``whispBoxControls 2``) with the MIB enum value ``fullReboot(2)``.
* The pre-fix code fell back to a typed ``dry_run=True`` because
  the gate referenced ``apply_oid`` (a method that never existed
  on the production client) AND because the production code
  opened the client via the read-only ``_client_factory`` (the
  ``V2CClient`` returned by the read-only factory does NOT
  implement ``WritableSnmpClient``).

The two RED regression tests below pin the wire contract. They
mirror the helpers used in ``test_migrate.py`` so the fixture
shape stays consistent across the two migration-path modules.

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no
real IPs, hostnames, serials, or credentials.
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


# Production reboot OID + reboot-vote OID per the production catalogs
# under ``data/oid-catalogs/cambium/pmp450i/*.source.json``.
REBOOT_OID_DOTTED: str = "1.3.6.1.4.1.161.19.3.3.3.2.0"
REBOOT_IF_REQUIRED_OID_DOTTED: str = "1.3.6.1.4.1.161.19.3.3.3.4.0"


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


def _build_catalog(firmware: str = "15.2.1") -> OidCatalogRegistry:
    """Catalog registry carrying the reboot OIDs from the production catalog."""
    oids: dict[str, str] = {
        "reboot": REBOOT_OID_DOTTED,
        "rebootIfRequired": REBOOT_IF_REQUIRED_OID_DOTTED,
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
    """Records every ``Device`` passed to the per-host client factory.

    Mirrors the ``_RecordingFactory`` from ``test_migrate.py``. Each
    factory call mints a fresh ``_WritableSnmpClientForHost`` and
    tracks it so the per-client ``set`` calls can be inspected after
    ``fetch_reboot`` returns.

    Issue #80: the factory returns a ``WritableSnmpClient``-shaped
    stub (with ``set()``) so the reboot path can actually exercise
    the wire code in tests. The ``rebootIfRequired`` GET path only
    uses ``get_oid`` so the additional ``set`` verb is a no-op for
    that path.
    """

    def __init__(self, *, sysdescr_per_host: dict[str, str] | None = None) -> None:
        self._sysdescr_per_host = sysdescr_per_host or {}
        self.calls: list[Device] = []
        self.hosts_probed: list[str] = []
        self.clients: list[_WritableSnmpClientForHost] = []

    def __call__(self, device: Any) -> Any:
        """Behaves like ``Pmp450iSnmpDriver._client_factory(device)``.

        Returns a tiny ``WritableSnmpClient``-shaped stub that
        records the per-host sysDescr GET and the ``set`` frames
        emitted by the reboot path. Issue #80: the contract is the
        real ``WritableSnmpClient`` (with ``set``); tests no longer
        define a synthetic ``apply_oid`` shim.
        """
        from typing import cast

        d = cast(Device, device)
        self.calls.append(d)
        host = str(getattr(d, "host", "?"))
        self.hosts_probed.append(host)
        client = _WritableSnmpClientForHost(
            host=host,
            sysdescr=self._sysdescr_per_host.get(host, ""),
        )
        self.clients.append(client)
        return client


class _WritableSnmpClientForHost:
    """Minimal ``WritableSnmpClient``-shaped stub for issue #80.

    Implements the full ``WritableSnmpClient`` Protocol contract
    (``get_oid``, ``walk``, ``close``, ``set``). Records every
    ``set(oid, value)`` call so the wire path can be asserted in
    tests, and returns a configurable sysDescr value from
    ``get_oid``. The reboot path reads ``rebootIfRequired`` via
    ``get_oid``; the value is the configured reboot vote (``0`` =
    not required, ``1`` = required).

    Issue #80: this stub REPLACES the prior ``apply_oid``-shaped
    client. Defining ``apply_oid`` here would reproduce the
    bug-masking behaviour we are removing.
    """

    def __init__(
        self,
        *,
        host: str,
        sysdescr: str,
        reboot_vote: int = 1,
    ) -> None:
        self._host = host
        self._sysdescr = sysdescr
        self._reboot_vote = reboot_vote
        self.close_calls: int = 0
        # Recorded as a list of (oid, value) tuples in call order.
        self.set_calls: list[tuple[str, str | int]] = []

    def get_oid(self, oid: str) -> str | int:
        # The ``rebootIfRequired`` GET path returns the configured
        # reboot vote. The pre-flight uses sysDescr (returns the
        # configured string). Both paths are read-only.
        if oid == REBOOT_IF_REQUIRED_OID_DOTTED:
            return self._reboot_vote
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
        # Issue #80: the reboot path now consumes
        # ``_writable_client_factory`` for the SET frame. Inject
        # the SAME recording factory so the test can observe
        # which factory was reached for which step and which
        # ``set`` frames were emitted.
        writable_client_factory=factory,
    )
    driver._runtime_settings = settings
    return driver


def _settings() -> Settings:
    """Build a Settings instance with the HITL signing key populated."""
    return Settings(
        _env_file=None,
        _env_file_encoding=None,
        nora_hitl_signing_key=SecretStr("test-snmp-reboot-issue80-hmac-key"),
    )


# ---------------------------------------------------------------------------
# Issue #80 — wire-path regression tests.
#
# These tests pin the Tier-1 contract: when an operator authorizes a
# reboot with a valid HITL token, the helper MUST emit a real SET frame
# against the reboot OID with the ``fullReboot(2)`` value (per Cambium
# WHISP-BOX-MIBV2-MIB ``whispBoxControls 2``: ``INTEGER { finishedReboot(0),
# reboot(1), fullReboot(2) }``). The pre-fix code fell back to a typed
# ``dry_run=True`` because the gate referenced a non-existent
# ``apply_oid`` method AND the production helper used the read-only
# ``_client_factory`` (whose ``V2CClient`` does NOT implement
# ``WritableSnmpClient``).
# ---------------------------------------------------------------------------


def test_fetch_reboot_emits_set_on_reboot_oid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #80: ``fetch_reboot`` MUST emit a real SET frame.

    Pins three contracts:

    1. The driver opens the client via ``_writable_client_factory``
       (NOT the read-only ``_client_factory``) for the SET path.
    2. The gate ``hasattr(client, "set")`` is True for a
       ``WritableSnmpClient``-shaped client; the dry-run fallback
       does NOT fire.
    3. The wire SET carries the ``fullReboot(2)`` value (the
       Cambium MIB enum, NOT a frequency) on the
       ``1.3.6.1.4.1.161.19.3.3.3.2.0`` OID.

    Pre-fix this test fails because the buggy code routed through
    ``_client_factory`` (read-only), which returns a stub WITHOUT
    ``set``, falling back to dry-run.
    """
    from nora.drivers.snmp_pmp450i import reboot as reboot_mod

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    # The factory mints a WritableSnmpClient-shaped stub. The
    # ``rebootIfRequired`` GET returns 1 (``rebootRequired``) so
    # the firmware-vote branch fires and the SET path is reached.
    factory = _RecordingFactory(sysdescr_per_host={"192.0.2.10": "Cambium PMP 450i AP 15.2.1"})
    settings = _settings()
    driver = _build_driver(inventory=inv, registry=registry, factory=factory, settings=settings)

    monkeypatch.setattr(reboot_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    import json

    from nora.hitl.tokens import mint_token

    token_obj = mint_token(
        "tester",
        ttl_seconds=900,
        signing_key=SecretStr("test-snmp-reboot-issue80-hmac-key"),
    )
    valid_token = json.dumps(token_obj.model_dump(mode="json"))

    result = reboot_mod.fetch_reboot(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        settings=settings,
    )

    # Contract 1: dry-run did NOT fire.
    assert result["dry_run"] is False, (
        f"Expected the real-SET path; got dry_run=True. would_set={result['would_set']!r}"
    )
    assert result["would_set"] == []

    # Contract 2 + 3: at least one of the recording clients emitted
    # a ``set`` against the reboot OID with the ``fullReboot(2)``
    # value. The reboot path opens the client via
    # ``_writable_client_factory``, which (in this test) is the
    # same recording factory — so we aggregate ``set_calls`` across
    # all clients the factory returned.
    expected_value = reboot_mod._REBOOT_VALUE_FULL  # 2 (fullReboot)
    all_set_calls = [call for client in factory.clients for call in client.set_calls]
    matching = [
        (oid, value)
        for oid, value in all_set_calls
        if oid == REBOOT_OID_DOTTED and value == expected_value
    ]
    assert matching, (
        f"Expected at least one ``set({REBOOT_OID_DOTTED!r}, {expected_value})`` on "
        f"the wire; got set_calls={all_set_calls!r}. "
        f"This is the issue #80 bug: production falls back to dry-run because "
        f"the gate references a method that does not exist."
    )

    # The RebootResult also reports the SET in ``set_calls`` for
    # audit-trail parity.
    assert REBOOT_OID_DOTTED in "\n".join(result["set_calls"])


def test_fetch_reboot_dry_run_falls_back_when_client_lacks_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dry-run seam stays: a client WITHOUT ``set`` falls back.

    Issue #80: the fix preserves the WU-3 dry-run contract — a
    client lacking the write verb (e.g. a thin read-only mock
    used by upstream tests that do NOT want to exercise SET
    frames) MUST still surface ``dry_run=True`` with the
    would-be SET pair in ``would_set``. Only the GATE reference
    changes from ``apply_oid`` (a non-existent verb) to ``set``
    (the real ``WritableSnmpClient`` verb).
    """
    from nora.drivers.snmp_pmp450i import reboot as reboot_mod

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
            # Pre-flight sysDescr returns a string; the reboot-vote
            # GET returns the integer ``1`` so the firmware-vote
            # branch fires and the SET path WOULD be reached.
            if oid == REBOOT_IF_REQUIRED_OID_DOTTED:
                return 1
            return "Cambium PMP 450i AP 15.2.1"

        def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
            return []

        def close(self) -> None:
            self.close_calls += 1

    ro_factory = _ReadOnlyFactory()
    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1")
    settings = _settings()
    driver = _build_driver(inventory=inv, registry=registry, factory=ro_factory, settings=settings)

    monkeypatch.setattr(reboot_mod, "save_intervention_record", lambda *a, **kw: {"status": "OK"})

    import json

    from nora.hitl.tokens import mint_token

    token_obj = mint_token(
        "tester",
        ttl_seconds=900,
        signing_key=SecretStr("test-snmp-reboot-issue80-hmac-key"),
    )
    valid_token = json.dumps(token_obj.model_dump(mode="json"))

    result = reboot_mod.fetch_reboot(
        driver=driver,
        device_id="ap-7400-01",
        approval_token=valid_token,
        settings=settings,
    )

    # Dry-run fired; would_set carries the would-be SET pair.
    # Pydantic v2 serializes the inner tuple as a list when the
    # field is accessed via ``__getitem__``; we assert the list
    # shape the runtime actually returns.
    assert result["dry_run"] is True
    assert result["would_set"] == [[REBOOT_OID_DOTTED, reboot_mod._REBOOT_VALUE_FULL]]
    assert result["set_calls"] == []
    assert result["rebooted"] is True
    assert result["reason"] == "reboot_required_by_firmware"


__all__ = [
    # Issue #80 wire-path regression tests.
    "test_fetch_reboot_emits_set_on_reboot_oid",
    "test_fetch_reboot_dry_run_falls_back_when_client_lacks_set",
]
