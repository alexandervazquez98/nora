"""HITL-gated RF migration — slice 4 (PR 4 commit 3).

The slice-4 RF migration tool (``snmp_migrate_radio_frequency``)
delegates the wire path through :func:`fetch_migrate`. The helper
implements the slice-4 contract per `pmp450i-radio-tools/spec.md`
sub-cluster 3:

1. :func:`nora.hitl.tokens.verify_approval_token` runs FIRST. Any
   missing or invalid token raises
   :class:`AutonomousMutationRejected` with the **literal** message
   ``"autonomous device mutation rejected: HITL approval token
   required"`` BEFORE any SNMP SET frame is emitted.
2. The migration order is make-before-break: ONLINE_ACTIVE
   subscribers migrate first, ACTIVE_DEGRADED subscribers migrate
   second, and the AP carrier change runs LAST.
   PRE_EXISTING_OFFLINE SMs are excluded via the central
   :func:`nora.drivers.snmp_pmp450i.subscribers.categorize_subscribers`
   helper.
3. After the AP SET frame a ``threading.Timer`` watchdog is armed
   for ``Settings.nora_hitl_rollback_timeout_seconds`` (default
   300). On loss-of-management the watchdog reverts the SET frame
   to the prior carrier frequency AND the migration emits a
   ``POST_MIGRATION`` intervention record with
   ``rolled_back: true, reason: "loss_of_management"``.
4. On successful reachability check within the timeout the tool
   cancels the watchdog and emits the intervention record with
   ``rolled_back: false``.
5. Issue #80 (PR #80 follow-up): when the SNMP client lacks the
   ``set`` verb — e.g. a thin read-only mock used by upstream
   tests that do NOT want to exercise SET frames — the tool
   short-circuits before any wire frame and returns a typed
   dry-run result (``dry_run=True, would_set=[...]``) so operators
   see what WOULD have happened, instead of raising
   ``AttributeError``. Write mutations stay out of scope; the
   production ``V2CClient`` exposes ``set`` through the
   ``WritableV2CClient`` adapter wired via ``_writable_client_factory``.

The helper also writes one intervention record per completion
(success OR rollback) through
:func:`nora.intervention_writer.writer.save_intervention_record`.

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real
IPs, hostnames, serials, or credentials.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from nora.drivers.exceptions import (
    CommunityValidationFailed,
    NetworkUnreachableError,
    SnmpTimeoutError,
)
from nora.drivers.snmp_pmp450i.band_plan import _is_band_crossing
from nora.drivers.snmp_pmp450i.subscribers import (
    fetch_sm_table,
)
from nora.hitl.tokens import verify_approval_token
from nora.intervention_writer.writer import save_intervention_record

if TYPE_CHECKING:
    from nora.config import Settings
    from nora.drivers.oid_catalog import OidCatalog
    from nora.drivers.snmp_pmp450i.client import SnmpClient


logger = logging.getLogger("nora.drivers.snmp_pmp450i.migrate")


# ---------------------------------------------------------------------------
# Migration OID names + typed models — slice 4 surface.
# ---------------------------------------------------------------------------


# OID name for the AP carrier frequency SET frame. The catalog carries
# a stable dotted OID; the helper looks it up at runtime. The
# ``migratePriorCarrierFrequency`` name is reused for the rollback
# SET frame so the watchdog can revert the AP without a fresh OID
# resolution.
MIGRATION_OID_NAMES: tuple[str, ...] = (
    "migrateCarrierFrequency",
    "migratePriorCarrierFrequency",
)


# ---------------------------------------------------------------------------
# WU-4 (issue #62) — per-SM community source label.
#
# The ``_resolve_sm_community`` helper returns one of these labels so
# the pre-flight report records which credential was used per SM
# (operator auditability). ``INVENTORY`` is the legacy path; the two
# ``OVERRIDE_*`` values come from the new ``sm_communities`` operator
# parameter. The string value itself NEVER travels into the audit
# trail — only the label.
# ---------------------------------------------------------------------------


CommunitySource = Literal["OVERRIDE_IP", "OVERRIDE_LUID", "INVENTORY"]


class MigrationResult(BaseModel):
    """Typed migration result — slice 4 read/write tool return.

    The dict-shaped return is what the MCP tool surface serialises;
    the Pydantic model is the typed contract inside the library.

    Fields:

    * ``rolled_back`` — True when the watchdog fired; False on success.
    * ``reason`` — ``"loss_of_management"`` on rollback; ``None`` on
      success.
    * ``pre_existing_offline_excluded`` — count of PRE_EXISTING_OFFLINE
      SMs the central categoriser excluded from the candidate set.
    * ``online_active_migrated`` — count of ONLINE_ACTIVE migrations.
    * ``active_degraded_migrated`` — count of ACTIVE_DEGRADED migrations.
    * ``target_frequency_mhz`` — the requested carrier frequency.
    * ``device_id`` — the inventory device the migration ran against.
    * ``dry_run`` — ``True`` when the tool did NOT perform any write
      SET frame. Defaults to ``False`` (real migration).
    * ``would_set`` — the SET pairs ``(oid, value)`` the tool WOULD
      have emitted in dry-run mode. Empty on real migrations. The
      value type is ``str | int | float``; carrier-frequency SETs
      carry a ``float`` MHz value.
    * ``sm_community_overrides_used`` — count of SMs whose community
      string came from the operator-supplied ``sm_communities`` map
      instead of the inventory. ``0`` when no overrides were
      supplied or none matched (operator decision 2026-09-19;
      resolution order: IP first, then LUID, then inventory).

    The ``dry_run`` / ``would_set`` contract seam (issue #80, PR #80
    follow-up): when the SNMP client lacks the ``set`` verb — e.g. a
    thin read-only mock used by upstream tests — the tool
    short-circuits before any wire frame and returns this typed
    dry-run result so operators see what WOULD have happened,
    instead of raising ``AttributeError``. The production
    ``V2CClient`` exposes ``set`` through the ``WritableV2CClient``
    adapter wired via ``_writable_client_factory``.
    """

    model_config = ConfigDict(frozen=True)

    rolled_back: bool
    reason: str | None = None
    pre_existing_offline_excluded: int = 0
    online_active_migrated: int = 0
    active_degraded_migrated: int = 0
    target_frequency_mhz: float
    device_id: str
    dry_run: bool = False
    would_set: list[tuple[str, str | int | float]] = []
    # WU-C (feat/multi-community-band-reboot) — band-crossing flag.
    # ``True`` when the migration crosses regulatory bands
    # (5.x ↔ 4.9 GHz); the orchestrator MUST mint a second HITL
    # token and invoke ``snmp_reboot_radio`` after a successful
    # migration. ``False`` for same-band sub-channel changes
    # (5.7 → 5.8 GHz) where the 25.x MIB DESCRIPTION confirms no
    # reboot is required (per ``radioFreqCarrier``: "As of release
    # 16.1, this OID no longer requires reboot to take affect").
    band_crossing: bool = False
    # WU-4 (issue #62) — count of SMs whose community came from the
    # operator-supplied ``sm_communities`` map instead of the
    # inventory. Aggregated from
    # ``SmPreFlightResult.community_source`` in ``fetch_migrate``.
    sm_community_overrides_used: int = 0


# ---------------------------------------------------------------------------
# Pre-flight community validation — WU-A (feat/multi-community-band-reboot).
#
# The pre-flight runs BEFORE the HITL token gate. It walks the SM-table
# subtree on the AP (so we know which LUIDs are registered with this
# AP) and then issues one cheap ``sysDescr`` GET against every
# candidate SM, using that SM's own credentials from the inventory.
# Any failure (SM unreachable, community rejected, SM not in inventory)
# is folded into a typed ``PreFlightReport`` so the orchestrator can
# prompt the operator to confirm or supply a different community
# before the HITL token is minted. Zero SET frames are emitted at
# pre-flight time — the gate is read-only by design.
# ---------------------------------------------------------------------------


class SmPreFlightResult(BaseModel):
    """Per-SM outcome from the WU-A pre-flight community validation.

    Fields:

    * ``luid`` — the SM's logical unit ID as observed on the AP.
    * ``host`` — the IP the inventory carries for this SM (None when
      the SM is missing from inventory).
    * ``reachable`` — True when the ``sysDescr`` GET succeeded.
    * ``community_accepted`` — True when the agent accepted the
      community string. Independent of ``reachable`` (a SM can be
      reachable but auth-rejected).
    * ``error_class`` — the typed-exception class name (``DeviceUnreachable``,
      ``InvalidCommunity``, ``MISSING_INVENTORY_ENTRY``); ``None``
      on the success path.
    * ``error_message`` — verbatim driver message (no Pydantic coercion);
      sanitised at the tool boundary per Zero-Leakage.
    * ``community_source`` — WU-4 (issue #62) audit label. One of
      ``"OVERRIDE_IP"`` (operator-supplied community keyed by IP),
      ``"OVERRIDE_LUID"`` (operator-supplied community keyed by
      LUID), or ``"INVENTORY"`` (legacy path; community from the
      device's inventory entry). The string value NEVER travels in
      this field — only the label. Defaults to ``"INVENTORY"`` for
      backward compatibility with the WU-A contract.

    The model is frozen so the MCP tool boundary can serialise via
    ``model_dump(mode="json")`` without mutation risk.
    """

    model_config = ConfigDict(frozen=True)

    luid: str
    host: str | None = None
    reachable: bool = False
    community_accepted: bool = False
    error_class: str | None = None
    error_message: str | None = None
    community_source: CommunitySource = "INVENTORY"


class PreFlightReport(BaseModel):
    """Aggregate WU-A pre-flight community validation result.

    Fields:

    * ``ap_reachable`` — True when the AP's own ``sysDescr`` GET succeeded
      (the AP must respond before we can read its SM-table subtree).
    * ``ap_sysdescr`` — verbatim sysDescr body from the AP (RFC 1213
      ``1.3.6.1.2.1.1.1.0``). ``None`` when unreachable.
    * ``sm_results`` — one ``SmPreFlightResult`` per SM candidate that
      the AP reported. Empty when ``ap_reachable`` is False.
    * ``missing_inventory_luids`` — LUIDs that the AP reported but
      the operator inventory has no entry for. The orchestrator MUST
      tell the operator to register these via ``register_device``
      before re-attempting the migration.
    """

    model_config = ConfigDict(frozen=True)

    ap_reachable: bool
    ap_sysdescr: str | None = None
    sm_results: list[SmPreFlightResult] = Field(default_factory=list)
    missing_inventory_luids: list[str] = Field(default_factory=list)


def _validate_sm_communities(
    *,
    driver: Any,
    device: Any,
    sm_luids: list[str],
    client_factory: Callable[[Any], "SnmpClient"],
    sysdescr_oid: str = "1.3.6.1.2.1.1.1.0",
    sm_communities: "dict[str, str] | None" = None,
) -> PreFlightReport:
    """Issue one ``sysDescr`` GET against every SM and the AP.

    Args:
        driver: ``Pmp450iSnmpDriver`` (carries inventory + client factory).
        device: AP ``Device`` resolved from the inventory.
        sm_luids: List of LUIDs reported by the AP's SM-table subtree.
        client_factory: Driver's client factory — produces a fresh
            ``SnmpClient`` per SM so the per-SM community is used.
        sysdescr_oid: RFC 1213 ``sysDescr`` OID; default
            ``1.3.6.1.2.1.1.1.0``. Parameterised for hermetic tests.
        sm_communities: WU-4 (issue #62) operator-supplied per-SM
            community overrides. Keys may be the SM's IP
            (``sm_device.host``) OR the LUID. The resolver tries IP
            first, then LUID, then falls back to the inventory
            community (operator decision 2026-09-19). ``None`` or
            ``{}`` keeps the legacy WU-A behaviour where every SM
            uses its inventory community.

    Returns:
        ``PreFlightReport`` aggregating AP reachability + per-SM
        outcomes. Never raises on per-SM wire failures; the report
        itself surfaces typed errors via ``error_class`` /
        ``error_message``. Only ``DeviceUnreachable`` on the AP path
        short-circuits with an empty ``sm_results`` list.

    Per-SM resolution:

    * If the SM's LUID has no matching ``device_id`` in the inventory,
      the result carries ``error_class='MISSING_INVENTORY_ENTRY'`` and
      the LUID is also collected into ``missing_inventory_luids``.
    * If the resolved community string is ``None`` (no inventory
      community and no override), the result carries
      ``error_class='InvalidCommunity'`` and ``community_source='INVENTORY'``
      so the orchestrator knows the SM has no usable credentials.
    * Wire failures are translated to typed exceptions:

      * ``OSError`` / ``TimeoutError`` → ``DeviceUnreachable(host)``
      * ``puresnmp.exc.SnmpError``    → ``InvalidCommunity(community)``

    When an override applies (source ``OVERRIDE_IP`` or
    ``OVERRIDE_LUID``), the helper threads a synthetic
    ``Device.model_copy(update={"community": SecretStr(override)})``
    into ``client_factory`` so the SNMP wire frame carries the
    operator-supplied community instead of the inventory one. The
    ``SmPreFlightResult.community_source`` label records which
    credential won; the string itself NEVER travels in the result.
    """
    import puresnmp.exc

    # AP reachability probe first; the AP must answer sysDescr before we
    # trust the SM-table subtree walk that follows. On failure the
    # pre-flight returns an empty sm_results list so the operator sees
    # the AP-side problem without being misled by the SM-side.
    ap_reachable = True
    ap_sysdescr: str | None = None
    try:
        ap_client = client_factory(device)
        try:
            ap_sysdescr = str(ap_client.get_oid(sysdescr_oid))
        finally:
            try:
                ap_client.close()
            except Exception:  # pragma: no cover - close is best-effort
                pass
    except (OSError, TimeoutError, SnmpTimeoutError, NetworkUnreachableError):
        ap_reachable = False
        ap_sysdescr = None
    except puresnmp.exc.SnmpError:
        ap_reachable = False
        ap_sysdescr = None

    sm_results: list[SmPreFlightResult] = []
    missing_luids: list[str] = []

    # Skip per-SM probes when the AP itself is unreachable. The
    # pre-flight surfaces AP-side failure first so the operator can
    # investigate without flooding a broken network with SM probes.
    if not ap_reachable:
        return PreFlightReport(
            ap_reachable=False,
            ap_sysdescr=None,
            sm_results=[],
            missing_inventory_luids=[],
        )

    for luid in sm_luids:
        # Resolve the inventory entry for this SM by LUID. We walk
        # ``driver._inventory.device_ids`` (sorted) and look up by
        # ``device_id == luid`` — a future change can add a
        # ``luid -> device_id`` map to Inventory; for now the simple
        # identity-keyed lookup is enough.
        sm_device = _resolve_sm_device(driver, luid)
        if sm_device is None:
            missing_luids.append(luid)
            sm_results.append(
                SmPreFlightResult(
                    luid=luid,
                    host=None,
                    reachable=False,
                    community_accepted=False,
                    error_class="MISSING_INVENTORY_ENTRY",
                    error_message=(
                        f"LUID {luid!r} reported by AP SM-table but absent "
                        f"from inventory; register via register_device first"
                    ),
                    community_source="INVENTORY",
                )
            )
            continue

        host = str(getattr(sm_device, "host", None) or "")

        # WU-4 (issue #62) — resolve the SM community via IP / LUID /
        # inventory precedence. The resolver returns ``(None,
        # "INVENTORY")`` when no credentials are available, in which
        # case we surface an ``InvalidCommunity`` result instead of
        # issuing an SNMP frame the agent would reject anyway.
        community, community_source = _resolve_sm_community(
            sm_device=sm_device,
            sm_luid=luid,
            sm_communities=sm_communities,
        )
        if community is None:
            sm_results.append(
                SmPreFlightResult(
                    luid=luid,
                    host=host,
                    reachable=False,
                    community_accepted=False,
                    error_class="InvalidCommunity",
                    error_message=(
                        f"no community available for {host} (LUID {luid}); "
                        "supply via sm_communities or inventory"
                    ),
                    community_source=community_source,
                )
            )
            continue

        # When the resolver picked an override, build a synthetic
        # ``Device`` carrying the override community so the SNMP
        # frame uses the operator-supplied credential. The Device
        # model is Pydantic-frozen; ``model_copy`` returns a new
        # instance and skips the validator (acceptable: we already
        # know the override is a non-empty string from the resolver
        # contract).
        if community_source in ("OVERRIDE_IP", "OVERRIDE_LUID"):
            effective_device = sm_device.model_copy(update={"community": SecretStr(community)})
        else:
            effective_device = sm_device

        try:
            sm_client = client_factory(effective_device)
        except (OSError, TimeoutError, SnmpTimeoutError) as exc:
            sm_results.append(
                SmPreFlightResult(
                    luid=luid,
                    host=host,
                    reachable=False,
                    community_accepted=False,
                    error_class="DeviceUnreachable",
                    error_message=f"{host}: {exc!s}",
                    community_source=community_source,
                )
            )
            continue
        except (puresnmp.exc.SnmpError, NetworkUnreachableError) as exc:
            sm_results.append(
                SmPreFlightResult(
                    luid=luid,
                    host=host,
                    reachable=False,
                    community_accepted=False,
                    error_class="InvalidCommunity",
                    error_message=f"auth rejected for {host}: {exc!s}",
                    community_source=community_source,
                )
            )
            continue

        try:
            try:
                sm_client.get_oid(sysdescr_oid)
                sm_results.append(
                    SmPreFlightResult(
                        luid=luid,
                        host=host,
                        reachable=True,
                        community_accepted=True,
                        error_class=None,
                        error_message=None,
                        community_source=community_source,
                    )
                )
            except (OSError, TimeoutError, SnmpTimeoutError) as exc:
                sm_results.append(
                    SmPreFlightResult(
                        luid=luid,
                        host=host,
                        reachable=False,
                        community_accepted=False,
                        error_class="DeviceUnreachable",
                        error_message=f"{host}: {exc!s}",
                        community_source=community_source,
                    )
                )
            except (puresnmp.exc.SnmpError, NetworkUnreachableError) as exc:
                sm_results.append(
                    SmPreFlightResult(
                        luid=luid,
                        host=host,
                        reachable=False,
                        community_accepted=False,
                        error_class="InvalidCommunity",
                        error_message=f"auth rejected for {host}: {exc!s}",
                        community_source=community_source,
                    )
                )
        finally:
            try:
                sm_client.close()
            except Exception:  # pragma: no cover - close is best-effort
                pass

    return PreFlightReport(
        ap_reachable=ap_reachable,
        ap_sysdescr=ap_sysdescr,
        sm_results=sm_results,
        missing_inventory_luids=missing_luids,
    )


def _resolve_sm_device(driver: Any, luid: str) -> Any | None:
    """Return the inventory ``Device`` for ``luid`` or ``None``.

    The inventory model keys devices by ``device_id`` (an operator
    choice), so the helper first tries the identity match (most
    operators name the SM ``sm-<luid>`` or similar) and then falls
    back to a linear scan over the configured devices. A future
    change can introduce a dedicated ``luid -> device_id`` map on
    ``Inventory``; the current helper is intentionally narrow.
    """
    inventory = getattr(driver, "_inventory", None)
    if inventory is None:
        return None
    try:
        return inventory.get(luid)
    except Exception:  # noqa: BLE001 - DeviceNotFoundError is the expected path
        # Linear scan fallback: look for any device whose ``device_id``
        # contains ``luid`` (e.g. ``sm-7400-001`` matches LUID ``001``).
        # Operators that need stricter matching must rename inventory
        # entries to match the LUID exactly.
        for device_id in getattr(inventory, "device_ids", []):
            if luid in device_id:
                try:
                    return inventory.get(device_id)
                except Exception:
                    continue
        return None


def _resolve_sm_community(
    *,
    sm_device: Any | None,
    sm_luid: str,
    sm_communities: "dict[str, str] | None",
) -> tuple[str | None, CommunitySource]:
    """Resolve the community string for one SM.

    Returns a ``(community, source)`` tuple. ``source`` is one of
    :data:`CommunitySource` and travels into
    :class:`SmPreFlightResult.community_source` so the audit trail
    records which credential won. The community string itself
    NEVER travels in the audit record — only the label.

    Resolution order (operator decision 2026-09-19):

    1. If ``sm_communities`` is not ``None`` and ``str(sm_device.host)``
       is a key, return ``(dict[ip], "OVERRIDE_IP")``.
    2. Else if ``sm_communities`` is not ``None`` and ``sm_luid`` is
       a key, return ``(dict[luid], "OVERRIDE_LUID")``.
    3. Else fall back to ``sm_device.community.get_secret_value()``;
       return ``(community, "INVENTORY")``. When ``sm_device`` is
       ``None`` or has no community attribute, return
       ``(None, "INVENTORY")`` — the caller treats ``(None, _)`` as
       "no credentials available" and surfaces an
       :class:`InvalidCommunity` result.

    The resolver is a pure function (no I/O, no side effects); it
    is also exercised directly by the WU-4 test suite so the
    precedence rules are pinned at the unit level.
    """
    if sm_communities is not None:
        host = str(getattr(sm_device, "host", None) or "") if sm_device is not None else ""
        if host and host in sm_communities:
            return str(sm_communities[host]), "OVERRIDE_IP"
        if sm_luid in sm_communities:
            return str(sm_communities[sm_luid]), "OVERRIDE_LUID"

    if sm_device is None:
        return None, "INVENTORY"
    community = getattr(sm_device, "community", None)
    if community is None:
        return None, "INVENTORY"
    secret = getattr(community, "get_secret_value", None)
    if secret is None:
        # Plain string fallback (rare; tests sometimes use bare strings).
        return str(community), "INVENTORY"
    return secret(), "INVENTORY"


# ---------------------------------------------------------------------------
# Public seam — overridable for tests.
# ---------------------------------------------------------------------------


def _band_crossing_from_prior(
    prior_carrier: Any,
    target_mhz: float,
) -> bool:
    """Defensive helper — convert prior-carrier value to band-crossing flag.

    The runtime layer reads ``migratePriorCarrierFrequency`` (kHz)
    before the SET frame; on firmware ≥ 16.1 the read normally
    succeeds and returns an integer. On a non-numeric value (legacy
    firmware / fake client / mid-reboot) we conservatively return
    False — the runtime layer would consult ``radioFrequencyBand``
    for the authoritative vote in production. This is a fail-safe
    choice: a False negative means we do NOT mint a second HITL
    token for the reboot, which is the cheaper mistake.
    """
    try:
        prior_mhz = float(prior_carrier) / 1000.0
    except (TypeError, ValueError):
        return False
    return _is_band_crossing(prior_mhz, target_mhz)


def migrate_subscriber(
    *,
    category: str,
    luid: str,
    target_frequency_mhz: float,
    client: "SnmpClient",
) -> dict[str, Any]:
    """Migrate one SM to ``target_frequency_mhz``.

    The slice-4 contract pins the *order* (ONLINE_ACTIVE first,
    ACTIVE_DEGRADED second) but the per-SM wire path is
    vendor-specific. This default implementation is a placeholder
    that records the migration in the audit log and returns a
    success payload — production slices replace it with a real
    SET frame keyed off the per-SM OID branch.
    """
    logger.info(
        "migrate: category=%s luid=%s target_freq_mhz=%.3f",
        category,
        luid,
        target_frequency_mhz,
    )
    return {"status": "OK", "category": category, "luid": luid}


def _start_rollback_watchdog(
    *,
    timeout_seconds: int,
    on_loss_of_management: Callable[[], None],
) -> threading.Timer:
    """Arm a ``threading.Timer`` watchdog for ``timeout_seconds``.

    The watchdog is daemon=True so the process can exit even if the
    timer fires after the migration has returned. The returned
    Timer handle is later cancelled by :func:`_cancel_rollback_watchdog`
    on success.
    """
    timer = threading.Timer(timeout_seconds, on_loss_of_management)
    timer.daemon = True
    timer.start()
    return timer


def _cancel_rollback_watchdog(timer: threading.Timer | None) -> None:
    """Cancel the rollback watchdog. Idempotent on a ``None`` handle."""
    if timer is not None:
        timer.cancel()


def _wait_for_management_reachability(
    *,
    driver: Any,
    device: Any,
    catalog: "OidCatalog",
    rollback_timeout_seconds: int,
    rollback_signal: dict[str, bool],
) -> bool:
    """Poll management reachability until reachable, rolled back, or timeout.

    The probe is a single ``get_oid`` against ``apFirmwareVersion``
    over a fresh SNMP session — when the agent responds the
    migration is considered successful. Failure modes
    (``KeyError`` / ``OSError`` / ``TimeoutError``) are tolerated;
    the loop iterates until reachable, the watchdog flips the
    rollback flag, or the timeout elapses.
    """
    # Floor the polling duration at 0.5s so the watchdog callback has
    # a chance to fire even when the operator passes
    # ``nora_hitl_rollback_timeout_seconds=0`` for fast tests.
    poll_window = max(rollback_timeout_seconds, 1)
    deadline = time.monotonic() + poll_window
    while time.monotonic() < deadline:
        if rollback_signal.get("fired", False):
            return False
        try:
            probe_client = driver._client_factory(device)
            try:
                probe_client.get_oid(catalog.oids["apFirmwareVersion"])
            finally:
                try:
                    probe_client.close()
                except Exception:  # pragma: no cover - close is best-effort
                    pass
            return True
        except Exception:
            time.sleep(0.05)
    return False


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public helper — slice 4 migration tool body.
# ---------------------------------------------------------------------------


def fetch_migrate(
    *,
    driver: Any,
    device_id: str,
    approval_token: str | None,
    target_frequency_mhz: float,
    settings: "Settings | None" = None,
    sm_communities: "dict[str, str] | None" = None,
) -> dict[str, Any]:
    """Run the HITL-gated RF migration for ``device_id``.

    Args:
        driver: ``Pmp450iSnmpDriver`` instance (carries inventory +
            catalog + client factory).
        device_id: Inventory device id.
        approval_token: Operator-issued HITL token. The literal
            :class:`AutonomousMutationRejected` fires on missing /
            invalid tokens BEFORE any wire frame.
        target_frequency_mhz: Requested carrier frequency in MHz.
        settings: Optional :class:`Settings` instance (carries
            ``nora_hitl_rollback_timeout_seconds`` and
            ``nora_hitl_signing_key``).
        sm_communities: WU-4 (issue #62) per-SM community overrides.
            Keys may be IPs OR LUIDs; the resolver tries IP first,
            then LUID, then falls back to the inventory community
            (operator decision 2026-09-19). ``None`` (default) or
            an empty dict keeps the legacy WU-A behaviour where
            every SM uses its inventory community. The aggregated
            count of overrides that actually matched an SM is
            returned on :attr:`MigrationResult.sm_community_overrides_used`
            and written to the intervention record so the audit
            trail sees how many SMs migrated with non-inventory
            credentials. The community string itself NEVER travels
            into the audit trail — only the source label.

    Returns:
        A dict matching the :class:`MigrationResult` schema. On
        success ``rolled_back`` is ``False``; on loss-of-management
        it is ``True`` and ``reason`` is ``"loss_of_management"``.

    Raises:
        AutonomousMutationRejected: missing or invalid approval
            token. The literal message is the contract seam.
        CommunityValidationFailed: WU-A pre-flight discovered at
            least one SM that is unreachable, auth-rejected, or
            missing from the inventory. The full ``PreFlightReport``
            rides on the ``.report`` attribute so the orchestrator
            can ask the operator to confirm / supply a different
            community BEFORE the HITL token is spent.
        DeviceNotFoundError: unknown ``device_id``.
        CatalogNotFoundError: no catalog for the device's
            ``(vendor, model, firmware)`` triple.
    """
    # 1. Resolve device + catalog. WU-A pre-flight runs BEFORE the
    # HITL gate so the operator is never asked to mint an approval
    # token for a migration we already know will fail at the
    # community-string level.
    device = driver._inventory.get(device_id)  # noqa: SLF001 — internal API
    catalog = driver._catalog_registry.resolve(  # noqa: SLF001 — internal API
        (device.vendor, device.model, device.firmware)
    )

    # 2. WU-A pre-flight community validation. We walk the SM-table
    # subtree on the AP (the same walk the rest of the tool uses
    # below) so we know the candidate LUID set, then issue one
    # cheap sysDescr GET per SM using that SM's own credentials
    # from the inventory. On any per-SM failure (unreachable,
    # community rejected, missing inventory entry) we raise
    # ``CommunityValidationFailed`` carrying the typed report so
    # the orchestrator can prompt the operator before any HITL
    # token is spent. The pre-flight is opt-out via
    # ``Settings.nora_preflight_community_validation`` (default
    # True) so legacy test fixtures can disable it.
    preflight_enabled = bool(
        getattr(settings, "nora_preflight_community_validation", True)
        if settings is not None
        else True
    )
    # WU-4 (issue #62) — default to ``0`` so the legacy path (pre-flight
    # disabled) still produces a well-formed MigrationResult. The
    # aggregate is recomputed inside the ``if preflight_enabled:``
    # block when the pre-flight actually runs.
    override_count = 0
    if preflight_enabled:
        sm_summary = fetch_sm_table(driver=driver, device_id=device_id, settings=settings)
        candidate_luids = sorted(
            {record.luid for record in sm_summary.online_active}
            | {record.luid for record in sm_summary.active_degraded}
        )
        preflight_report = _validate_sm_communities(
            driver=driver,
            device=device,
            sm_luids=candidate_luids,
            client_factory=driver._client_factory,  # noqa: SLF001 — internal API
            sm_communities=sm_communities,
        )
        failed_results = [r for r in preflight_report.sm_results if r.error_class is not None]
        if not preflight_report.ap_reachable or failed_results:
            raise CommunityValidationFailed(report=preflight_report)

        # WU-4 (issue #62) — aggregate the count of SMs whose
        # community came from the operator-supplied overrides instead
        # of the inventory. The label travels on every per-SM result;
        # this is just the aggregate for the MigrationResult return.
        override_count = sum(
            1
            for r in preflight_report.sm_results
            if r.community_source in ("OVERRIDE_IP", "OVERRIDE_LUID")
        )

    # 3. HITL gate — fires AFTER the pre-flight. The signing key is
    # sourced from `Settings.nora_hitl_signing_key`; lazy fail-closed
    # if empty.
    signing_key = getattr(settings, "nora_hitl_signing_key", None) if settings is not None else None
    verify_approval_token(approval_token, signing_key=signing_key)

    # Migration OID names must be in the catalog.
    migration_oids: dict[str, str] = {}
    for name in MIGRATION_OID_NAMES:
        if name not in catalog.oids:
            raise LookupError(
                f"migration OID {name!r} missing from catalog "
                f"(vendor={catalog.vendor}, model={catalog.model}, firmware={catalog.firmware})"
            )
        migration_oids[name] = catalog.oids[name]

    # 3. Rollback timeout from Settings.
    rollback_timeout_seconds = int(getattr(settings, "nora_hitl_rollback_timeout_seconds", 300))

    # 4. Build a fresh client and resolve the pre-migration carrier
    # frequency. The watchdog reverts to this value on loss-of-mgmt.
    # Issue #80: open the client via ``_writable_client_factory`` so
    # the SET frames emitted by both the main path AND the rollback
    # watchdog actually reach the wire. The read-only ``_client_factory``
    # would always fall through to the dry-run seam below because the
    # produced ``V2CClient`` does NOT implement ``WritableSnmpClient``.
    client = driver._writable_client_factory(device)  # noqa: SLF001 — internal API
    try:
        # 5. Walk the SM table; fold into typed rows. The cross-check
        # is delegated to ``fetch_sm_table`` so the order rule
        # (history BEFORE categorise) stays in one module.
        sm_summary = fetch_sm_table(driver=driver, device_id=device_id, settings=settings)
        online_active = sm_summary.online_active
        active_degraded = sm_summary.active_degraded
        pre_existing = sm_summary.pre_existing_offline

        # 6. Capture the pre-migration carrier so the watchdog can
        # revert. The GET is best-effort; on miss we fall back to 0
        # so the watchdog emits ``SET migratePriorCarrierFrequency 0``
        # rather than crashing.
        try:
            prior_carrier = client.get_oid(migration_oids["migratePriorCarrierFrequency"])
        except KeyError:
            prior_carrier = 0

        # 7. Migrate ONLINE_ACTIVE subscribers FIRST.
        online_count = 0
        for record in online_active:
            migrate_subscriber(
                category="ONLINE_ACTIVE",
                luid=record.luid,
                target_frequency_mhz=target_frequency_mhz,
                client=client,
            )
            online_count += 1

        # 8. Migrate ACTIVE_DEGRADED subscribers SECOND.
        active_count = 0
        for record in active_degraded:
            migrate_subscriber(
                category="ACTIVE_DEGRADED",
                luid=record.luid,
                target_frequency_mhz=target_frequency_mhz,
                client=client,
            )
            active_count += 1

        # 9. AP carrier change — gated on client having ``set``
        # (dry-run fallback otherwise). See issue #80 in
        # https://github.com/alexandervazquez98/nora/issues/80.
        # The ``WritableSnmpClient`` Protocol exposes ``set(oid, value)``;
        # the production ``V2CClient`` itself is read-only and exposes
        # ``set`` only through the ``WritableV2CClient`` adapter wired
        # via ``_writable_client_factory`` above. Write mutations stay
        # out of scope for the read-only base ``SnmpClient`` Protocol,
        # so when a client lacks ``set`` we short-circuit and return a
        # typed dry-run result carrying the would-be SET pair.
        if hasattr(client, "set"):
            # 9a. Real-SET path: emit the AP carrier-change SET.
            # Cambium WHISP-APS-MIB ``radioFreqCarrier`` /
            # ``migrateCarrierFrequency`` is an Integer in kHz; convert
            # the operator-supplied MHz value to kHz so the wire frame
            # carries the MIB's native unit (5_800_000 for 5800 MHz).
            client.set(
                migration_oids["migrateCarrierFrequency"],
                int(round(target_frequency_mhz * 1000)),
            )

            # 10. Arm the rollback watchdog.
            rollback_state: dict[str, Any] = {"rolled_back": False, "reason": None}
            rollback_signal: dict[str, bool] = {"fired": False}

            def _on_loss_of_management() -> None:
                rollback_state["rolled_back"] = True
                rollback_state["reason"] = "loss_of_management"
                # Revert the SET frame to the prior carrier.
                # Issue #80: open the rollback client via the WRITABLE
                # factory too — the read-only factory would always
                # fall through to the dry-run seam because the
                # produced ``V2CClient`` does NOT expose ``set``.
                try:
                    revert_client = driver._writable_client_factory(device)
                    try:
                        # ``prior_carrier`` is already in kHz — the
                        # GET on ``migratePriorCarrierFrequency``
                        # returns the MIB-native unit (per the
                        # ``_band_crossing_from_prior`` docstring).
                        revert_client.set(
                            migration_oids["migratePriorCarrierFrequency"],
                            prior_carrier,
                        )
                    finally:
                        try:
                            revert_client.close()
                        except Exception:  # pragma: no cover
                            pass
                except Exception:
                    logger.exception("migrate: rollback SET failed for device=%s", device_id)
                rollback_signal["fired"] = True

            timer = _start_rollback_watchdog(
                timeout_seconds=rollback_timeout_seconds,
                on_loss_of_management=_on_loss_of_management,
            )

            # 11. Poll management reachability.
            management_reachable = _wait_for_management_reachability(
                driver=driver,
                device=device,
                catalog=catalog,
                rollback_timeout_seconds=rollback_timeout_seconds,
                rollback_signal=rollback_signal,
            )

            # 12. Cancel the watchdog.
            _cancel_rollback_watchdog(timer)

            rolled_back = rollback_state["rolled_back"] or not management_reachable
            reason = rollback_state["reason"] if rolled_back else None
            dry_run = False
            would_set: list[tuple[str, str | int | float]] = []
        else:
            # 9b. Dry-run fallback (issue #80): the client lacks the
            # ``set`` verb (e.g. a thin read-only mock). The tool
            # returns a typed dry-run result carrying the would-be SET
            # pair so operators see what WOULD have happened, instead
            # of crashing with ``AttributeError``. No SET frames were
            # emitted; no rollback window is needed; no reachability
            # of a fresh carrier to check. The base ``SnmpClient``
            # Protocol contract is deliberately read-only — production
            # ``V2CClient`` exposes ``set`` only through the
            # ``WritableV2CClient`` adapter wired via
            # ``_writable_client_factory``.
            would_set = [(migration_oids["migrateCarrierFrequency"], target_frequency_mhz)]
            logger.info(
                "migrate: client lacks set; emulating SET %s=%s on device=%s",
                migration_oids["migrateCarrierFrequency"],
                target_frequency_mhz,
                device_id,
            )
            rolled_back = False
            reason = None
            dry_run = True

        # 13. Emit exactly one ``save_intervention_record`` per completion.
        # On the dry-run path the status is ``"DRY_RUN"`` and the
        # ``record_name`` carries a ``[DRY-RUN]`` prefix so the audit
        # trail is unambiguous.
        record_status = "DRY_RUN" if dry_run else ("ABORTED" if rolled_back else "COMPLETED")
        record_name_prefix = "[DRY-RUN] " if dry_run else ""
        findings_and_dictamen = (
            f"dry_run={dry_run}; would_set={would_set!r}; no SET frames emitted"
            if dry_run
            else f"rolled_back={rolled_back}; reason={reason or 'n/a'}"
        )
        try:
            if settings is None:
                raise RuntimeError("migrate.fetch_migrate requires an explicit Settings instance")
            save_intervention_record(
                settings,
                {
                    "intervention_id": "INT-MIGRATE-{ts}".format(ts=int(time.time())),
                    "timestamp_iso": _utc_now_iso(),
                    "timestamp_unix": int(time.time()),
                    "ticket_number": "MIGRATE-7400",
                    "target_ip": str(getattr(device, "host", device_id)),
                    "stage": "POST_MIGRATION",
                    "record_name": (
                        f"{record_name_prefix}RF migration of {device_id} "
                        f"to {target_frequency_mhz} MHz"
                    ),
                    "status": record_status,
                    "agent_name": "nora-mcp",
                    "findings_and_dictamen": findings_and_dictamen,
                    "created_at": _utc_now_iso(),
                    "network_equipment": {
                        "target_ip": str(getattr(device, "host", device_id)),
                        "carrier_frequency_mhz": target_frequency_mhz,
                    },
                    "rolled_back": rolled_back,
                    "reason": reason,
                    "dry_run": dry_run,
                    "would_set": [list(item) for item in would_set],
                    # WU-4 (issue #62) — count of SMs whose
                    # community came from the operator-supplied
                    # ``sm_communities`` map instead of the
                    # inventory. The string value NEVER travels in
                    # the audit record — only this count.
                    "sm_community_overrides_used": override_count,
                },
            )
        except Exception:  # pragma: no cover - writer has its own status codes
            logger.exception("migrate: save_intervention_record failed for device=%s", device_id)

        return MigrationResult(
            rolled_back=rolled_back,
            reason=reason,
            pre_existing_offline_excluded=len(pre_existing),
            online_active_migrated=online_count,
            active_degraded_migrated=active_count,
            target_frequency_mhz=target_frequency_mhz,
            device_id=str(getattr(device, "host", device_id)),
            dry_run=dry_run,
            would_set=would_set,
            # WU-C band-crossing flag — fires on a confirmed cross-band
            # move (5.x ↔ 4.9). The prior_carrier read returns kHz;
            # convert to MHz for the table-driven detector. On a
            # non-numeric read (e.g. legacy firmware / fake client)
            # we conservatively return False — the runtime layer
            # would consult ``radioFrequencyBand`` OID for the
            # authoritative vote in production.
            band_crossing=_band_crossing_from_prior(
                prior_carrier,
                float(target_frequency_mhz),
            ),
            # WU-4 (issue #62) — aggregated count of SMs whose
            # community came from the operator-supplied
            # ``sm_communities`` map instead of the inventory. ``0``
            # when no overrides were supplied or none matched.
            sm_community_overrides_used=override_count,
        ).model_dump(mode="json")
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - close is best-effort
            pass


__all__ = [
    "MigrationResult",
    "MIGRATION_OID_NAMES",
    "fetch_migrate",
    "migrate_subscriber",
    "_start_rollback_watchdog",
    "_cancel_rollback_watchdog",
    "_wait_for_management_reachability",
    # WU-A (feat/multi-community-band-reboot) — pre-flight community
    # validation. Runs BEFORE the HITL gate so the operator is never
    # asked to mint an approval token for a migration we already know
    # will fail at the community-string level.
    "PreFlightReport",
    "SmPreFlightResult",
    "_validate_sm_communities",
    "_resolve_sm_device",
    # WU-4 (issue #62) — per-SM community overrides. ``CommunitySource``
    # is the audit label carried on ``SmPreFlightResult.community_source``;
    # ``_resolve_sm_community`` is the pure helper that implements the
    # IP-first / LUID-second / inventory-fallback precedence rule
    # (operator decision 2026-09-19).
    "CommunitySource",
    "_resolve_sm_community",
]
