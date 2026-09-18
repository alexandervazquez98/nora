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
5. WU-3 (PR #44 follow-ups): when the SNMP client lacks
   ``apply_oid`` — e.g. the production ``V2CClient`` whose
   read-only ``SnmpClient`` Protocol is deliberate — the tool
   short-circuits before any wire frame and returns a typed
   dry-run result (``dry_run=True, would_set=[...]``) so operators
   see what WOULD have happened, instead of raising
   ``AttributeError``. Write mutations stay out of scope.

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
from typing import TYPE_CHECKING, Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from nora.drivers.exceptions import CommunityValidationFailed
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

    The ``dry_run`` / ``would_set`` contract seam (WU-3, PR #44
    follow-ups): when the SNMP client lacks ``apply_oid`` — e.g. the
    production ``V2CClient`` whose read-only ``SnmpClient`` Protocol
    is deliberate — the tool short-circuits before any wire frame
    and returns this typed dry-run result so operators see what
    WOULD have happened, instead of raising ``AttributeError``.
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
    * Wire failures are translated to typed exceptions:

      * ``OSError`` / ``TimeoutError`` → ``DeviceUnreachable(host)``
      * ``puresnmp.exc.SnmpError``    → ``InvalidCommunity(community)``
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
    except (OSError, TimeoutError):
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
                )
            )
            continue

        host = str(getattr(sm_device, "host", None) or "")

        try:
            sm_client = client_factory(sm_device)
        except (OSError, TimeoutError) as exc:
            sm_results.append(
                SmPreFlightResult(
                    luid=luid,
                    host=host,
                    reachable=False,
                    community_accepted=False,
                    error_class="DeviceUnreachable",
                    error_message=f"{host}: {exc!s}",
                )
            )
            continue
        except puresnmp.exc.SnmpError as exc:
            sm_results.append(
                SmPreFlightResult(
                    luid=luid,
                    host=host,
                    reachable=False,
                    community_accepted=False,
                    error_class="InvalidCommunity",
                    error_message=f"auth rejected for {host}: {exc!s}",
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
                    )
                )
            except (OSError, TimeoutError) as exc:
                sm_results.append(
                    SmPreFlightResult(
                        luid=luid,
                        host=host,
                        reachable=False,
                        community_accepted=False,
                        error_class="DeviceUnreachable",
                        error_message=f"{host}: {exc!s}",
                    )
                )
            except puresnmp.exc.SnmpError as exc:
                sm_results.append(
                    SmPreFlightResult(
                        luid=luid,
                        host=host,
                        reachable=False,
                        community_accepted=False,
                        error_class="InvalidCommunity",
                        error_message=f"auth rejected for {host}: {exc!s}",
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


# ---------------------------------------------------------------------------
# Public seam — overridable for tests.
# ---------------------------------------------------------------------------


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
        )
        failed_results = [r for r in preflight_report.sm_results if r.error_class is not None]
        if not preflight_report.ap_reachable or failed_results:
            raise CommunityValidationFailed(report=preflight_report)

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
    client = driver._client_factory(device)  # noqa: SLF001 — internal API
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

        # 9. AP carrier change — gated on client having apply_oid
        # (dry-run fallback otherwise). See WU-3 in
        # odd/tasks/pr44-followups.md. The production ``V2CClient``
        # read-only ``SnmpClient`` Protocol contract is deliberate;
        # write mutations stay out of scope, so when the client lacks
        # ``apply_oid`` we short-circuit and return a typed dry-run
        # result carrying the would-be SET pair.
        if hasattr(client, "apply_oid"):
            # 9a. Real-SET path: emit the AP carrier-change SET.
            client.apply_oid(migration_oids["migrateCarrierFrequency"], target_frequency_mhz)

            # 10. Arm the rollback watchdog.
            rollback_state: dict[str, Any] = {"rolled_back": False, "reason": None}
            rollback_signal: dict[str, bool] = {"fired": False}

            def _on_loss_of_management() -> None:
                rollback_state["rolled_back"] = True
                rollback_state["reason"] = "loss_of_management"
                # Revert the SET frame to the prior carrier.
                try:
                    revert_client = driver._client_factory(device)
                    try:
                        revert_client.apply_oid(
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
            # 9b. Dry-run fallback (WU-3): the production ``V2CClient``
            # Protocol contract is read-only. The tool returns a typed
            # dry-run result carrying the would-be SET pair so operators
            # see what WOULD have happened, instead of crashing with
            # ``AttributeError``. No SET frames were emitted; no
            # rollback window is needed; no reachability of a fresh
            # carrier to check.
            would_set = [(migration_oids["migrateCarrierFrequency"], target_frequency_mhz)]
            logger.info(
                "migrate: client lacks apply_oid; emulating SET %s=%s on device=%s",
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
]
