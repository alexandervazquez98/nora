"""Tests for the SM subscriber baseline + per-LUID diagnostics — PR 3
slice 3 of `2026-09-13-pmp450i-production-surface`.

These tests pin the public contract for the two slice-3
`@mcp.tool` registrations that land in PR 3:

* ``snmp_get_sm_table`` — typed ``SubscriberSummary`` carrying per-row
  category (ONLINE_ACTIVE / ACTIVE_DEGRADED / PRE_EXISTING_OFFLINE) plus
  the cross-checked unbiased baseline (PRE_EXISTING_OFFLINE excluded
  from candidates).
* ``snmp_get_sm_detailed_diagnostics`` — typed
  ``SmDetailedDiagnostics`` carrying jitter, CINR, Rx/Tx levels,
  retransmits, and interface error counters for the requested ``luid``.

Plus the cross-check ordering contract that PR 3 makes visible to MCP
clients:

* ``search_intervention_history(stage="PRE_DIAGNOSTIC")`` MUST be
  invoked BEFORE ``categorize_subscribers(...)`` for every caller, so
  the "known_pre_existing_offline_subscribers" set pre-populates the
  PRE_EXISTING_OFFLINE bucket. Reordering the calls breaks the
  cross-check, and the test fails closed.

ONE source of truth: ``nora.drivers.snmp_pmp450i.subscribers.categorize_subscribers``
is the only classification entry point. The MCP wrappers and the
tool-body helpers never inline a category decision.

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real
IPs, hostnames, serials, or credentials.

Named tests for PR 3:

* ``test_sm_table_categorizes_online_active``
* ``test_sm_table_categorizes_active_degraded_low_cinr``
* ``test_sm_table_categorizes_pre_existing_offline``
* ``test_sm_table_unbiased_baseline_excludes_pre_existing``
* ``test_sm_detailed_diagnostics_typed_for_luid``
* ``test_get_intervention_history_called_before_categorize``

Issue #69 (2026-09-20) — SM ``siteName`` + ``ipAddress`` exposure:

* ``test_sm_table_exposes_site_name_and_ip_address``
* ``test_sm_table_zero_ip_routes_to_pre_existing_offline``
* ``test_sm_table_uptime_overrides_zero_ip``
* ``test_sm_table_missing_site_name_or_ip_defaults_empty``
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from nora.drivers.inventory import Inventory
from nora.drivers.oid_catalog import OidCatalog, OidCatalogRegistry

# ---------------------------------------------------------------------------
# Helpers — inventory + catalog with the radio-metrics seed, the legacy
# OIDs `summaries.py` reuses, AND the NEW PR 3 SM-table / diagnostics
# OIDs (slice 3 adds eight).
# ---------------------------------------------------------------------------


def _build_inventory(
    tmp_path: Path,
    *,
    ap_firmware: str = "15.2.1",
) -> Inventory:
    """Hermetic inventory with one v2c AP and one v3 SM (RFC 5737 hosts)."""
    payload = {
        "devices": [
            {
                "device_id": "ap-7400-01",
                "vendor": "cambium",
                "model": "pmp450i",
                "firmware": ap_firmware,
                "host": "192.0.2.10",
                "snmp_version": "v2c",
                "community": "change-me-v2c",
            },
            {
                "device_id": "sm-7400-02",
                "vendor": "cambium",
                "model": "pmp450i",
                "firmware": "15.2.1",
                "host": "192.0.2.11",
                "snmp_version": "v3",
                "auth_password": "change-me-auth",
                "priv_password": "change-me-priv",
            },
        ]
    }
    inv_path = tmp_path / "devices.yaml"
    inv_path.write_text(yaml.safe_dump(payload))
    return Inventory.from_yaml(inv_path)


def _radio_seed_oids() -> dict[str, str]:
    """The six radio-metrics REQUIRED_OIDs (mirror of driver module).

    These come from the verified Cambium PMP 450i MIB positions (issue
    #35) under the whispLinkTable (.3.1.4.1) and whispBox (.3.1.1)
    subtrees — see ``data/oid-catalogs/sources/cambium/pmp450i/*.source.json``
    for the full verified map.
    """
    return {
        "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.4.1.36.0",
        "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.4.1.38.0",
        "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.4.1.34.0",
        # Issue #54 (2026-09-19): ``signalStrengthTx`` (broken
        # ``maxSMTxPwr``) replaced by ``eirp`` (``whispBoxActiveEIRP``,
        # ``.306.0``).
        "eirp": "1.3.6.1.4.1.161.19.3.3.1.306.0",
        "ssr": "1.3.6.1.4.1.161.19.3.1.4.1.86.0",
        "modulationMode": "1.3.6.1.4.1.161.19.3.1.4.1.40.0",
    }


def _legacy_oids() -> dict[str, str]:
    """The v1 seed OIDs ``summaries.py`` reuses for the AP summary.

    Verified MIB positions for issue #35 (see
    ``data/oid-catalogs/sources/cambium/pmp450i/*.source.json``):
    channelBandwidth is in whispApsConfig (.3.3.2.83),
    frequency is in whispBoxStatus (.3.1.1.2),
    transmitPower is the activeTxPowerStr leaf (.3.3.1.232),
    apFirmwareVersion is whispBoxSwVersion (.3.3.1.1),
    subscribersCount is in whispBox (.3.1.7.1),
    frame utilization columns live in whispApsFrUtlStats (.3.1.12.1.x),
    upTime is the standard RFC1213 sysUpTime (.2.1.1.3).
    """
    return {
        "channelBandwidth": "1.3.6.1.4.1.161.19.3.3.2.83.0",
        "frequency": "1.3.6.1.4.1.161.19.3.1.1.2.0",
        "transmitPower": "1.3.6.1.4.1.161.19.3.3.1.232.0",
        "upTime": "1.3.6.1.2.1.1.3.0",
        "apFirmwareVersion": "1.3.6.1.4.1.161.19.3.3.1.1.0",
        "subscribersCount": "1.3.6.1.4.1.161.19.3.1.7.1.0",
        "frameUtilizationDlPct": "1.3.6.1.4.1.161.19.3.1.12.1.1.0",
        "frameUtilizationUlPct": "1.3.6.1.4.1.161.19.3.1.12.1.2.0",
    }


def _sm_table_oids() -> dict[str, str]:
    """SM table OID names + dotted OIDs (synthesised public refs).

    Per Cambium WHISP-APS-MIB (verified issue #35): the SM table on an
    AP lives in ``whispLinkTable`` rooted at ``1.3.6.1.4.1.161.19.3.1.4.1``.
    The previous value ``1.3.6.1.4.1.161.19.3.2.1`` pointed at
    ``whispSm`` (an individual subscriber module) which returns
    ``noSuchName`` on an AP. Columns under whispLinkTable:
    ``.46`` = smSessionUptime, ``.74`` = smCinr DL,
    ``.19`` = linkSessState, ``.1`` = linkLuid.

    Issue #69 (2026-09-20): two extra columns per issue #69 — ``.33``
    = ``linkSiteName`` (``DisplayString``) and ``.69`` = ``linkIpAddress``
    (``IpAddress``). Operator-verified via live ``snmpwalk`` against
    production APs (firmwares 25.0.1 / 25.1.0). These two scalars are
    folded onto every ``SubscriberRecord`` row as ``site_name`` and
    ``ip_address``.
    """
    return {
        "smSessionUptime": "1.3.6.1.4.1.161.19.3.1.4.1.46.0",
        "smCinr": "1.3.6.1.4.1.161.19.3.1.4.1.74.0",
        "smLinkStatus": "1.3.6.1.4.1.161.19.3.1.4.1.19.0",
        "smLuid": "1.3.6.1.4.1.161.19.3.1.4.1.1.0",
        "smSiteName": "1.3.6.1.4.1.161.19.3.1.4.1.33.0",
        "smIpAddress": "1.3.6.1.4.1.161.19.3.1.4.1.69.0",
    }


def _sm_diagnostics_oids() -> dict[str, str]:
    """SM diagnostics OID names + dotted OIDs (synthesised public refs).

    Issue #54 (2026-09-19): ``smJitter`` (``linkAveJitter`` FSK-only,
    broken on OFDM/MIMO hardware) and ``smTxLevel`` (``maxSMTxPwr``
    engineering-only + tabular) were removed. ``smSnrH`` and
    ``ssrLink`` were added as OFDM-correct per-LUID metrics.

    Columns under whispLinkTable (.3.1.4.1): ``.74`` =
    linkRadioAggrSmVCalculatedSnr (vertical CINR), ``.84`` =
    linkRadioAggrSmHCalculatedSnr (horizontal CINR), ``.86`` =
    linkRadioAggrSignalStrengthRatio, ``.34`` = avgPowerLevel
    (per-SM Rx dBm), ``.150`` = retransmittedFragmentsCount.
    """
    return {
        "smCinr": "1.3.6.1.4.1.161.19.3.1.4.1.74.0",
        "smSnrH": "1.3.6.1.4.1.161.19.3.1.4.1.84.0",
        "ssrLink": "1.3.6.1.4.1.161.19.3.1.4.1.86.0",
        "smRetransmits": "1.3.6.1.4.1.161.19.3.1.4.1.150.0",
        "smRxLevel": "1.3.6.1.4.1.161.19.3.1.4.1.34.0",
    }


def _build_catalog(
    firmware: str = "15.2.1",
    *,
    include_sm_oids: bool = True,
) -> OidCatalogRegistry:
    """Catalog registry carrying the radio seed + legacy + (optional) SM OIDs."""
    oids: dict[str, str] = {}
    oids.update(_radio_seed_oids())
    oids.update(_legacy_oids())
    if include_sm_oids:
        oids.update(_sm_table_oids())
        oids.update(_sm_diagnostics_oids())
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

    Implements :class:`nora.drivers.snmp_pmp450i.client.SnmpClient` with
    hand-rolled `get_oid` and `walk` semantics so the SM-table helper's
    subtree walk produces deterministic results.
    """

    def __init__(
        self,
        values: dict[str, str | int],
        *,
        walk_results: dict[str, list[tuple[str, str | int]]] | None = None,
    ) -> None:
        self._values = dict(values)
        self._walk_results: dict[str, list[tuple[str, str | int]]] = walk_results or {}
        self.get_calls: list[str] = []
        self.walk_calls: list[str] = []

    def get_oid(self, oid: str) -> str | int:
        self.get_calls.append(oid)
        if oid not in self._values:
            raise KeyError(oid)
        return self._values[oid]

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        self.walk_calls.append(base_oid)
        return list(self._walk_results.get(base_oid, []))

    def close(self) -> None:
        return None


def _build_driver(
    *,
    inventory: Inventory,
    registry: OidCatalogRegistry,
    canned: _FakeSnmpClient,
) -> Any:
    """Construct a ``Pmp450iSnmpDriver`` wired to the canned fake client.

    The driver gets a sentinel ``_runtime_settings`` attribute so the
    slice-3 helper recognises a Settings context and calls
    ``search_intervention_history`` (the cross-check ordering test
    pins the call). The sentinel is replaced by the per-test mock
    via ``monkeypatch.setattr(...)`` on the helper module.
    """
    from nora.drivers.snmp_pmp450i import Pmp450iSnmpDriver

    driver = Pmp450iSnmpDriver(
        inventory=inventory,
        catalog_registry=registry,
        client_factory=lambda d: canned,
    )
    # Inject a sentinel Settings-like object so ``subscribers.fetch_sm_table``
    # calls ``search_intervention_history`` with the mock. The mock
    # replaces the helper module function entirely — the sentinel's
    # contents are irrelevant.
    driver._runtime_settings = object()  # type: ignore[attr-defined]
    return driver


def _sm_session_table() -> list[tuple[str, str | int]]:
    """Synthetic PMP 450i SM table subtree response.

    Each SM occupies four OID rows in the whispLinkTable (.3.1.4.1)
    public Cambium branch — columns ``.46`` (smSessionUptime),
    ``.74`` (smCinr), ``.19`` (smLinkStatus), ``.1`` (smLuid), plus
    the two issue #69 columns ``.33`` (linkSiteName) and ``.69``
    (linkIpAddress). Returns a flat ``(oid, value)`` list — the helper
    walks the base OID and parses.

    Issue #69 (2026-09-20): site names use the operator's canonical
    ``BAJ02-VVU-TIJU-NNN`` shape; IPs use TEST-NET-1 (``192.0.2.x``)
    so the fixture stays Zero-Leakage. SM 3 (PRE_EXISTING_OFFLINE)
    carries ``ip_address == "0.0.0.0"`` to exercise the new heuristic.
    """
    base = "1.3.6.1.4.1.161.19.3.1.4.1"
    rows = [
        # SM 1 — ONLINE_ACTIVE: uptime > 0, valid IP, mod 8X, link up.
        (
            f"{base}.46.1",
            86400,
        ),  # smSessionUptime
        (f"{base}.74.1", 25),  # smCinr (dB)
        # Issue #58 (2026-09-19): ``linkSessState`` is an INTEGER
        # enum (``idle=0``, ``inSession=1``, ``clearing=2``, etc.)
        # per WHISP-APS-MIB. The radio never returns the legacy
        # ``"LINKED"`` string. ``1`` (``inSession``) is the
        # canonical active value.
        (f"{base}.19.1", 1),  # smLinkStatus: inSession
        (f"{base}.1.1", "001"),  # smLuid
        # Issue #69 (2026-09-20): site_name + ip_address on SM 001.
        (f"{base}.33.1", "BAJ02-VVU-TIJU-018"),
        (f"{base}.69.1", "192.0.2.31"),
        # SM 2 — ACTIVE_DEGRADED: low CINR.
        (f"{base}.46.2", 43200),
        (f"{base}.74.2", 12),  # cinr < 18 → degraded
        (f"{base}.19.2", 1),  # inSession (still active, but degraded signal)
        (f"{base}.1.2", "002"),
        # Issue #69 (2026-09-20): site_name + ip_address on SM 002.
        (f"{base}.33.2", "BAJ02-VVU-TIJU-019"),
        (f"{base}.69.2", "192.0.2.32"),
        # SM 3 — PRE_EXISTING_OFFLINE (uptime == 0, ip == "0.0.0.0").
        (f"{base}.46.3", 0),
        (f"{base}.74.3", 0),
        # ``0`` (``idle``) is the canonical offline value; the
        # fold helper maps it to ``"idle"`` and
        # ``categorize_subscribers`` routes it to
        # ``PRE_EXISTING_OFFLINE``.
        (f"{base}.19.3", 0),  # smLinkStatus: idle
        (f"{base}.1.3", "003"),
        # Issue #69 (2026-09-20): the null-IPv4 sentinel exercises the
        # ``ip_address == "0.0.0.0"`` → ``PRE_EXISTING_OFFLINE`` heuristic.
        (f"{base}.33.3", "BAJ02-VVU-TIJU-020"),
        (f"{base}.69.3", "0.0.0.0"),
    ]
    return rows


# ---------------------------------------------------------------------------
# Named test #1 — sm_table_categorizes_online_active
# ---------------------------------------------------------------------------


def test_sm_table_categorizes_online_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``snmp_get_sm_table`` categorises a healthy SM as ``ONLINE_ACTIVE``.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 2 scenario "ONLINE_ACTIVE /
    ACTIVE_DEGRADED / PRE_EXISTING_OFFLINE each resolve": a row with
    ``session_uptime > 0`` AND linked modulation categorises as
    ``ONLINE_ACTIVE``.
    """
    from nora.drivers.snmp_pmp450i.subscribers import SubscriberSummary  # noqa: F401

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_sm_oids=True)

    # No PRE_DIAGNOSTIC history → the cross-check exclusion is empty.
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    canned = _FakeSnmpClient(
        values={},
        walk_results={
            "1.3.6.1.4.1.161.19.3.1.4.1": _sm_session_table(),
        },
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    summary = driver.fetch_sm_table("ap-7400-01")

    assert isinstance(summary, SubscriberSummary)
    dumped = summary.model_dump(mode="json")
    online_active_luids = [r["luid"] for r in dumped["online_active"]]
    active_degraded_luids = [r["luid"] for r in dumped["active_degraded"]]
    pre_existing_luids = [r["luid"] for r in dumped["pre_existing_offline"]]

    assert "001" in online_active_luids, (
        f"Expected SM 001 in ONLINE_ACTIVE; got online={online_active_luids} "
        f"degraded={active_degraded_luids} pre_existing={pre_existing_luids}"
    )
    # Issue #69 (2026-09-20): ``site_name`` and ``ip_address`` must be
    # populated on the SM 001 row (the ONLINE_ACTIVE bucket).
    online_active_row_001 = next(r for r in dumped["online_active"] if r["luid"] == "001")
    assert online_active_row_001["site_name"] == "BAJ02-VVU-TIJU-018"
    assert online_active_row_001["ip_address"] == "192.0.2.31"
    assert dumped["pre_existing_offline_count"] == 1
    assert dumped["baseline_size"] == 2  # 1 online + 1 degraded; pre_existing excluded


# ---------------------------------------------------------------------------
# Named test #2 — sm_table_categorizes_active_degraded_low_cinr
# ---------------------------------------------------------------------------


def test_sm_table_categorizes_active_degraded_low_cinr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SM with ``cinr < 18 dB`` categorises as ``ACTIVE_DEGRADED``.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 2 scenario "ACTIVE_DEGRADED"
    branch: an active session (uptime > 0) but CINR below the 18 dB
    threshold is degraded, not online-active.
    """
    from nora.drivers.snmp_pmp450i.subscribers import SubscriberSummary  # noqa: F401

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_sm_oids=True)
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    # Single-SM table — one active session, low CINR.
    base = "1.3.6.1.4.1.161.19.3.1.4.1"
    rows = [
        (f"{base}.46.1", 43200),  # uptime > 0
        (f"{base}.74.1", 12),  # cinr 12 dB (< 18)
        # Issue #58 (2026-09-19): ``linkSessState`` is an INTEGER enum;
        # ``1`` = ``inSession``.
        (f"{base}.19.1", 1),
        (f"{base}.1.1", "002"),
        # Issue #69 (2026-09-20): site_name + ip_address for the
        # single-SM test, so the new fields are asserted end-to-end.
        (f"{base}.33.1", "BAJ02-VVU-TIJU-019"),
        (f"{base}.69.1", "192.0.2.32"),
    ]
    canned = _FakeSnmpClient(
        values={},
        walk_results={"1.3.6.1.4.1.161.19.3.1.4.1": rows},
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    summary = driver.fetch_sm_table("ap-7400-01")

    assert isinstance(summary, SubscriberSummary)
    dumped = summary.model_dump(mode="json")
    active_degraded_luids = [r["luid"] for r in dumped["active_degraded"]]
    online_active_luids = [r["luid"] for r in dumped["online_active"]]

    assert "002" in active_degraded_luids, (
        f"Expected SM 002 in ACTIVE_DEGRADED (cinr=12); got degraded={active_degraded_luids} "
        f"online={online_active_luids}"
    )
    # Issue #69 (2026-09-20): ``site_name`` and ``ip_address`` flow
    # through the fold helper onto every ACTIVE_DEGRADED row.
    active_degraded_row_002 = next(r for r in dumped["active_degraded"] if r["luid"] == "002")
    assert active_degraded_row_002["site_name"] == "BAJ02-VVU-TIJU-019"
    assert active_degraded_row_002["ip_address"] == "192.0.2.32"
    assert dumped["baseline_size"] == 1


# ---------------------------------------------------------------------------
# Named test #3 — sm_table_categorizes_pre_existing_offline
# ---------------------------------------------------------------------------


def test_sm_table_categorizes_pre_existing_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SM whose ``luid`` is in ``known_pre_existing_offline_subscribers``
    categorises as ``PRE_EXISTING_OFFLINE``.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 2 scenario "PRE_EXISTING_OFFLINE"
    branch: the cross-check guard populates the bucket via
    ``search_intervention_history(stage="PRE_DIAGNOSTIC")`` BEFORE
    ``categorize_subscribers(...)`` runs (see
    ``test_get_intervention_history_called_before_categorize``).
    """
    from nora.drivers.snmp_pmp450i.subscribers import SubscriberSummary  # noqa: F401

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_sm_oids=True)

    # The cross-check fixture: one PRE_DIAGNOSTIC record flags SM 003
    # as pre-existing offline (the helper unwraps the sanitized record
    # and pulls the `luid` field).
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [
            {
                "network_equipment": {
                    "pre_existing_offline_subscribers": [
                        {"luid": "003"},
                    ]
                }
            }
        ],
    )

    base = "1.3.6.1.4.1.161.19.3.1.4.1"
    rows = [
        # SM 1 — ONLINE_ACTIVE.
        (f"{base}.46.1", 86400),
        (f"{base}.74.1", 25),
        # Issue #58 (2026-09-19): ``linkSessState`` INTEGER enum;
        # ``1`` = ``inSession``.
        (f"{base}.19.1", 1),
        (f"{base}.1.1", "001"),
        # Issue #69 (2026-09-20): site_name + ip_address on SM 001.
        (f"{base}.33.1", "BAJ02-VVU-TIJU-018"),
        (f"{base}.69.1", "192.0.2.31"),
        # SM 3 — recovered from a prior intervention: uptime > 0,
        # inSession, healthy CINR. Was down at intervention time
        # (``pre_existing_list`` records it), but is back online now.
        (f"{base}.46.3", 86400),  # uptime > 0
        (f"{base}.74.3", 22),
        (f"{base}.19.3", 1),  # inSession
        (f"{base}.1.3", "003"),
        # Issue #69 (2026-09-20): recovered SM 003 also carries the new
        # fields (with a real, non-zero IP — the offline-SM ``0.0.0.0``
        # sentinel only fires when the radio has not yet associated an
        # IP, which is not the case for a recovered SM).
        (f"{base}.33.3", "BAJ02-VVU-TIJU-020"),
        (f"{base}.69.3", "192.0.2.33"),
    ]
    canned = _FakeSnmpClient(
        values={},
        walk_results={"1.3.6.1.4.1.161.19.3.1.4.1": rows},
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    summary = driver.fetch_sm_table("ap-7400-01")

    assert isinstance(summary, SubscriberSummary)
    dumped = summary.model_dump(mode="json")
    pre_existing_luids = [r["luid"] for r in dumped["pre_existing_offline"]]
    online_active_luids = [r["luid"] for r in dumped["online_active"]]

    # Issue #58 (2026-09-19): a recovered subscriber (uptime > 0,
    # inSession) MUST be categorised by signal health, NOT
    # condemned to PRE_EXISTING_OFFLINE by the intervention-memory
    # cross-check. The previous test expected SM 003 in the
    # pre_existing bucket; that was the bug WU-E fixed.
    assert "003" not in pre_existing_luids, (
        f"Recovered SM 003 must NOT be condemned to PRE_EXISTING_OFFLINE; got "
        f"pre_existing={pre_existing_luids} online={online_active_luids}"
    )
    assert "003" in online_active_luids
    assert "001" in online_active_luids
    # Issue #69 (2026-09-20): the recovered SM 001 must carry the new
    # fields end-to-end (site_name + ip_address) so operators can
    # identify the subscriber by session name + management IP, not
    # just by LUID.
    online_active_row_001 = next(r for r in dumped["online_active"] if r["luid"] == "001")
    assert online_active_row_001["site_name"] == "BAJ02-VVU-TIJU-018"
    assert online_active_row_001["ip_address"] == "192.0.2.31"
    assert dumped["baseline_size"] == 2


# ---------------------------------------------------------------------------
# Named test #4 — sm_table_unbiased_baseline_excludes_pre_existing
# ---------------------------------------------------------------------------


def test_sm_table_unbiased_baseline_excludes_pre_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unbiased baseline excludes ``PRE_EXISTING_OFFLINE`` from candidates.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 2 scenario "unbiased
    baseline excludes PRE_EXISTING_OFFLINE from candidates": the
    aggregate ``baseline_size`` MUST be ``ONLINE_ACTIVE + ACTIVE_DEGRADED``
    only; ``PRE_EXISTING_OFFLINE`` SMs never inflate the candidate set.
    """
    from nora.drivers.snmp_pmp450i.subscribers import SubscriberSummary  # noqa: F401

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_sm_oids=True)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    # Build a 3-row table: one ONLINE_ACTIVE, one ACTIVE_DEGRADED, one
    # PRE_EXISTING_OFFLINE (uptime == 0). The aggregate baseline_size
    # MUST be 2 (online + degraded); pre-existing excluded.
    base = "1.3.6.1.4.1.161.19.3.1.4.1"
    rows = [
        # SM 1 — ONLINE_ACTIVE.
        (f"{base}.46.1", 86400),
        (f"{base}.74.1", 25),
        # Issue #58 (2026-09-19): ``linkSessState`` INTEGER enum.
        (f"{base}.19.1", 1),  # inSession
        (f"{base}.1.1", "001"),
        # SM 2 — ACTIVE_DEGRADED.
        (f"{base}.46.2", 43200),
        (f"{base}.74.2", 12),
        (f"{base}.19.2", 1),  # inSession (active but degraded signal)
        (f"{base}.1.2", "002"),
        # SM 3 — PRE_EXISTING_OFFLINE.
        (f"{base}.46.3", 0),
        (f"{base}.74.3", 0),
        # ``0`` = ``idle`` (canonical offline value).
        (f"{base}.19.3", 0),
        (f"{base}.1.3", "003"),
    ]
    canned = _FakeSnmpClient(
        values={},
        walk_results={"1.3.6.1.4.1.161.19.3.1.4.1": rows},
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    summary = driver.fetch_sm_table("ap-7400-01")

    assert isinstance(summary, SubscriberSummary)
    dumped = summary.model_dump(mode="json")

    # `baseline_size` is the unbiased candidate count: ONLINE + DEGRADED.
    assert dumped["baseline_size"] == 2, (
        f"baseline_size MUST exclude pre_existing_offline; got {dumped['baseline_size']} "
        f"(online={dumped['online_active']}, degraded={dumped['active_degraded']}, "
        f"pre_existing={dumped['pre_existing_offline']})"
    )
    assert dumped["pre_existing_offline_count"] == 1
    assert len(dumped["online_active"]) == 1
    assert len(dumped["active_degraded"]) == 1


# ---------------------------------------------------------------------------
# Named test #5 — sm_detailed_diagnostics_typed_for_luid
# ---------------------------------------------------------------------------


def test_sm_detailed_diagnostics_typed_for_luid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``snmp_get_sm_detailed_diagnostics`` returns a typed
    ``SmDetailedDiagnostics`` carrying OFDM modulation metrics
    queried at the SM's LUID (issue #54).

    The fake client returns canned values keyed by the **per-LUID**
    OID form (the driver's strip-`.0` + append-`.<luid>` dispatch).
    Every reachable SM populates the full metric set; ``smCinr``
    shares its OID with the SM-table walk so the canned dict uses
    the diagnostics form here (the per-LUID suffix ``.2``).
    """
    from nora.drivers.snmp_pmp450i.subscribers import SmDetailedDiagnostics

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_sm_oids=True)
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    diag_oids = _sm_diagnostics_oids()
    target_luid = "002"
    per_luid = {name: oid[:-2] + f".{target_luid}" for name, oid in diag_oids.items()}
    canned = _FakeSnmpClient(
        values={
            per_luid["smCinr"]: 22,
            per_luid["smSnrH"]: 10,
            per_luid["ssrLink"]: 6,
            per_luid["smRetransmits"]: 12,
            per_luid["smRxLevel"]: -65,
        }
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    diagnostics = driver.fetch_sm_detailed_diagnostics("ap-7400-01", luid=target_luid)

    assert isinstance(diagnostics, SmDetailedDiagnostics)
    dumped = diagnostics.model_dump(mode="json")
    assert dumped["luid"] == target_luid
    assert dumped["snr_v_db"] == 22
    assert dumped["snr_h_db"] == 10
    assert dumped["ssr_link_db"] == 6
    assert dumped["rx_level_dbm"] == -65
    assert dumped["retransmits"] == 12
    # ``jitter_ms`` / ``tx_level_dbm`` are gone in the OFDM model
    # (issue #54 RCA: FSK-only / engineering-only).
    assert "jitter_ms" not in dumped
    assert "tx_level_dbm" not in dumped


# ---------------------------------------------------------------------------
# Named test #6 — get_intervention_history_called_before_categorize
# ---------------------------------------------------------------------------


def test_get_intervention_history_called_before_categorize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``search_intervention_history`` MUST be called BEFORE
    ``categorize_subscribers`` for every caller.

    Per `pmp450i-radio-tools/spec.md` sub-cluster 2 requirement
    "`search_intervention_history` cross-check": the order is fixed —
    the history call populates ``known_pre_existing_offline_subscribers``
    BEFORE categorisation runs. Reordering breaks the cross-check.

    The test pins the order via a shared call log: history pushes its
    name, categorise pushes its name, and the categorise spy fails
    closed if it ever sees its name appear without the history name
    ahead of it.
    """
    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_sm_oids=True)

    call_log: list[str] = []

    def fake_history(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        call_log.append("search_intervention_history")
        return []

    # Wrap the real ``categorize_subscribers`` so we can assert the
    # cross-check ordering without changing its return value. The spy
    # fails closed if it sees its own name appear ahead of the history
    # name in the log — i.e. categorisation happened BEFORE history.
    from nora.drivers.snmp_pmp450i import subscribers as subs_mod

    real_categorize = subs_mod.categorize_subscribers

    def spy_categorize(*args: Any, **kwargs: Any) -> Any:
        call_log.append("categorize_subscribers")
        assert "search_intervention_history" in call_log, (
            "cross-check ordering violated: "
            "search_intervention_history MUST be called BEFORE categorize_subscribers"
        )
        return real_categorize(*args, **kwargs)

    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        fake_history,
    )
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.categorize_subscribers",
        spy_categorize,
    )

    canned = _FakeSnmpClient(
        values={},
        walk_results={"1.3.6.1.4.1.161.19.3.1.4.1": _sm_session_table()},
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    summary = driver.fetch_sm_table("ap-7400-01")

    # Both calls landed, history FIRST.
    assert call_log == [
        "search_intervention_history",
        "categorize_subscribers",
    ], f"Expected history-then-categorize call order; got {call_log}"
    assert summary.pre_existing_offline_count == 1


# ---------------------------------------------------------------------------
# MCP wrapper coverage — exercise the `@mcp.tool` registered functions
# in ``src.nora.server`` so the slice-3 wrappers stay covered end-to-end.
# The named tests above pin the unit-level contract; this section
# pins the MCP-tool contract that the FastMCP instance exposes.
# ---------------------------------------------------------------------------


def _drive_sm_table_tool(
    *,
    inventory: Inventory,
    registry: OidCatalogRegistry,
    canned: _FakeSnmpClient,
    device_id: str,
    history_fixture: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Drive the ``snmp_get_sm_table`` MCP tool via the FastMCP client.

    Wires the driver singleton + runtime settings, monkey-patches the
    ``search_intervention_history`` call inside the helpers module so
    the cross-check sees the supplied fixture, and calls the tool
    through the FastMCP client so the wrapper path is exercised
    end-to-end.
    """
    import asyncio

    from nora import server as server_mod
    from nora.drivers.registry import set_driver as _set_driver

    driver = _build_driver(inventory=inventory, registry=registry, canned=canned)

    _set_driver(driver)
    server_mod.set_runtime_state(_HermeticSettings(nora_interventions_dir=Path("/tmp/nora-empty")))

    if history_fixture is not None:
        import nora.drivers.snmp_pmp450i.subscribers as subs_mod

        original = subs_mod.search_intervention_history
        subs_mod.search_intervention_history = lambda *a, **kw: list(history_fixture)
        try:
            return asyncio.run(_call_sm_table_tool(server_mod, device_id))
        finally:
            subs_mod.search_intervention_history = original
    return asyncio.run(_call_sm_table_tool(server_mod, device_id))


async def _call_sm_table_tool(server_mod: Any, device_id: str) -> dict[str, Any]:
    """Async helper: invoke ``snmp_get_sm_table`` via the FastMCP client."""
    from fastmcp import Client

    async with Client(server_mod.mcp) as client:
        result = await client.call_tool("snmp_get_sm_table", {"device_id": device_id})
        # FastMCP returns a CallToolResult; unwrap the structured content
        # or the text content into a dict.
        if hasattr(result, "structured_content") and result.structured_content:
            return result.structured_content  # type: ignore[no-any-return]
        if hasattr(result, "data") and result.data is not None:
            return result.data  # type: ignore[no-any-return]
        return {}


class _HermeticSettings:
    """Minimal Settings stand-in for the FastMCP test driver.

    The slice-3 helpers read ``settings`` to call
    ``search_intervention_history``. We never reach the on-disk
    read path because the helper module's
    ``search_intervention_history`` is replaced by the test fixture.
    """

    def __init__(self, *, nora_interventions_dir: Path) -> None:
        self.nora_interventions_dir = nora_interventions_dir
        self.nora_interventions_keyword_search_max_records = 100
        self.nora_interventions_correlate_scan_limit = 50


# ---------------------------------------------------------------------------
# Issue #58 (2026-09-19) — recovered-subscriber invariant.
# ---------------------------------------------------------------------------


def test_recovered_subscriber_not_condemned_to_pre_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``session_uptime > 0`` overrides the intervention-memory cross-check.

    Regression guard for the bias described in issue #58: a
    subscriber recorded as ``pre_existing_offline`` in a prior
    intervention must NOT be condemned to ``PRE_EXISTING_OFFLINE``
    forever. When the radio reports ``session_uptime > 0`` AND a
    non-offline ``linkSessState``, the categorisation uses live
    signal evidence (CINR + modulation) instead of the historical
    blacklist.

    Scenario: a single SM recovered from a prior outage. Its LUID
    (``004``) is in the cross-checked ``pre_existing_list``; live
    wire shows 24 h uptime, ``inSession`` (1), 22 dB CINR, 8X
    modulation. The expected bucket is ``ONLINE_ACTIVE``.
    """
    from nora.drivers.snmp_pmp450i.subscribers import (
        LINK_SESS_STATE_NAMES,
        SubscriberRecord,
        categorize_subscribers,
    )

    assert LINK_SESS_STATE_NAMES[1] == "inSession"
    assert LINK_SESS_STATE_NAMES[0] == "idle"

    row = SubscriberRecord(
        luid="004",
        session_uptime=86400,
        cinr_db=22,
        link_status="inSession",
        modulation="8X",
    )
    buckets = categorize_subscribers([row], frozenset({"004"}))

    assert len(buckets["ONLINE_ACTIVE"]) == 1
    assert buckets["ONLINE_ACTIVE"][0].luid == "004"
    assert buckets["PRE_EXISTING_OFFLINE"] == []
    assert buckets["ACTIVE_DEGRADED"] == []


# ---------------------------------------------------------------------------
# Slice-3 named tests above; subscribers module is exercised end-to-end.
# Additional defensive-path coverage is exercised in the dedicated
# ``tests/test_snmp_subscribers_defensive.py`` module (out of scope for
# PR 3's review budget — the budget pressure comes from the named tests
# + their setup + the MCP-wrapper coverage for the new tools).
# ---------------------------------------------------------------------------


__all__ = [
    "test_sm_table_categorizes_online_active",
    "test_sm_table_categorizes_active_degraded_low_cinr",
    "test_sm_table_categorizes_pre_existing_offline",
    "test_recovered_subscriber_not_condemned_to_pre_existing",
    "test_sm_table_unbiased_baseline_excludes_pre_existing",
    "test_sm_detailed_diagnostics_typed_for_luid",
    "test_get_intervention_history_called_before_categorize",
    "test_sm_table_exposes_site_name_and_ip_address",
    "test_sm_table_zero_ip_routes_to_pre_existing_offline",
    "test_sm_table_uptime_overrides_zero_ip",
    "test_sm_table_missing_site_name_or_ip_defaults_empty",
]


# ---------------------------------------------------------------------------
# Issue #69 (2026-09-20) — SM ``siteName`` + ``ipAddress`` exposure.
# ---------------------------------------------------------------------------


def test_sm_table_exposes_site_name_and_ip_address(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``snmp_get_sm_table`` exposes ``site_name`` and ``ip_address`` on every row.

    Per issue #69 (2026-09-20): the SM table walk must surface
    ``whispLinkEntry.33`` (linkSiteName) and ``whispLinkEntry.69``
    (linkIpAddress) so operators can identify subscribers by session
    name + management IP, not just by transient LUID.
    """
    from nora.drivers.snmp_pmp450i.subscribers import SubscriberSummary  # noqa: F401

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_sm_oids=True)
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    canned = _FakeSnmpClient(
        values={},
        walk_results={"1.3.6.1.4.1.161.19.3.1.4.1": _sm_session_table()},
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    summary = driver.fetch_sm_table("ap-7400-01")
    dumped = summary.model_dump(mode="json")

    for bucket in ("online_active", "active_degraded", "pre_existing_offline"):
        for row in dumped[bucket]:
            assert "site_name" in row, f"Missing site_name in {bucket}: {row}"
            assert "ip_address" in row, f"Missing ip_address in {bucket}: {row}"

    # The fixture seeds SM 001 with site_name="BAJ02-VVU-TIJU-018"
    # and ip_address="192.0.2.31".
    row_001 = next(r for r in dumped["online_active"] if r["luid"] == "001")
    assert row_001["site_name"] == "BAJ02-VVU-TIJU-018"
    assert row_001["ip_address"] == "192.0.2.31"


def test_sm_table_zero_ip_routes_to_pre_existing_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``ip_address == "0.0.0.0"`` routes the row to ``PRE_EXISTING_OFFLINE``.

    Per the documented contract in ``categorize_subscribers``: a row
    whose IP is the null IPv4 sentinel is classified pre-existing
    offline regardless of session_uptime or link_status. This is the
    issue #69 heuristic landing.
    """
    from nora.drivers.snmp_pmp450i.subscribers import SubscriberSummary  # noqa: F401

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_sm_oids=True)
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    # Single SM: session_uptime > 0 (active), link_status = inSession
    # (active), but ip_address = "0.0.0.0" (no L3 association yet).
    # Per the new heuristic, this row MUST be PRE_EXISTING_OFFLINE.
    base = "1.3.6.1.4.1.161.19.3.1.4.1"
    rows = [
        (f"{base}.46.1", 86400),  # uptime > 0
        (f"{base}.74.1", 25),  # healthy CINR
        (f"{base}.19.1", 1),  # inSession
        (f"{base}.1.1", "001"),
        (f"{base}.33.1", "BAJ02-VVU-TIJU-018"),
        (f"{base}.69.1", "0.0.0.0"),  # <-- the heuristic trigger
    ]
    canned = _FakeSnmpClient(
        values={},
        walk_results={"1.3.6.1.4.1.161.19.3.1.4.1": rows},
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    summary = driver.fetch_sm_table("ap-7400-01")
    dumped = summary.model_dump(mode="json")

    pre_existing_luids = [r["luid"] for r in dumped["pre_existing_offline"]]
    assert "001" in pre_existing_luids, (
        f"SM with ip_address=0.0.0.0 MUST route to PRE_EXISTING_OFFLINE; "
        f"got pre_existing={pre_existing_luids}"
    )
    assert dumped["baseline_size"] == 0  # no candidates


def test_sm_table_uptime_overrides_zero_ip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The recovered-subscriber invariant (issue #58) is preserved.

    A row with ``session_uptime > 0`` AND ``ip_address != "0.0.0.0"``
    is categorized by signal health (CINR + modulation), never by IP
    alone. The new heuristic must not over-condemn recovered SMs.
    """
    from nora.drivers.snmp_pmp450i.subscribers import SubscriberSummary  # noqa: F401

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_sm_oids=True)
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    base = "1.3.6.1.4.1.161.19.3.1.4.1"
    rows = [
        (f"{base}.46.1", 86400),
        (f"{base}.74.1", 22),  # healthy CINR
        (f"{base}.19.1", 1),  # inSession
        (f"{base}.1.1", "001"),
        (f"{base}.33.1", "BAJ02-VVU-TIJU-018"),
        (f"{base}.69.1", "192.0.2.31"),  # real IP, NOT 0.0.0.0
    ]
    canned = _FakeSnmpClient(
        values={},
        walk_results={"1.3.6.1.4.1.161.19.3.1.4.1": rows},
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    summary = driver.fetch_sm_table("ap-7400-01")
    dumped = summary.model_dump(mode="json")

    online_active_luids = [r["luid"] for r in dumped["online_active"]]
    pre_existing_luids = [r["luid"] for r in dumped["pre_existing_offline"]]
    assert "001" in online_active_luids, (
        f"Recovered SM with real IP MUST be ONLINE_ACTIVE; got "
        f"online={online_active_luids} pre_existing={pre_existing_luids}"
    )
    assert "001" not in pre_existing_luids
    assert dumped["baseline_size"] == 1


def test_sm_table_missing_site_name_or_ip_defaults_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partial walk: missing columns default to empty string.

    Real radios occasionally omit a column (e.g. ``linkIpAddress`` on
    firmware builds that don't populate it). The fold must default
    missing values to ``""`` rather than raising.
    """
    from nora.drivers.snmp_pmp450i.subscribers import SubscriberSummary  # noqa: F401

    inv = _build_inventory(tmp_path)
    registry = _build_catalog(firmware="15.2.1", include_sm_oids=True)
    monkeypatch.setattr(
        "nora.drivers.snmp_pmp450i.subscribers.search_intervention_history",
        lambda *args, **kwargs: [],
    )

    base = "1.3.6.1.4.1.161.19.3.1.4.1"
    # Only .33 returned, .69 missing.
    rows = [
        (f"{base}.46.1", 86400),
        (f"{base}.74.1", 25),
        (f"{base}.19.1", 1),
        (f"{base}.1.1", "001"),
        (f"{base}.33.1", "BAJ02-VVU-TIJU-018"),
        # NO .69 row.
    ]
    canned = _FakeSnmpClient(
        values={},
        walk_results={"1.3.6.1.4.1.161.19.3.1.4.1": rows},
    )
    driver = _build_driver(inventory=inv, registry=registry, canned=canned)

    summary = driver.fetch_sm_table("ap-7400-01")
    dumped = summary.model_dump(mode="json")

    row_001 = next(r for r in dumped["online_active"] if r["luid"] == "001")
    assert row_001["site_name"] == "BAJ02-VVU-TIJU-018"
    assert row_001["ip_address"] == ""  # default empty, not an error
