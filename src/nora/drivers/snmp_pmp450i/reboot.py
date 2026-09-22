"""HITL-gated reboot tool — WU-C (feat/multi-community-band-reboot).

The slice-4 reboot tool (``snmp_reboot_radio``) delegates the wire
path through :func:`fetch_reboot`. The helper implements the
WU-C contract per `odd/tasks/multi-community-migration-and-band-reboot.md`:

1. :func:`nora.hitl.tokens.verify_approval_token` runs FIRST. Any
   missing or invalid token raises
   :class:`AutonomousMutationRejected` with the **literal** message
   ``"autonomous device mutation rejected: HITL approval token
   required"`` BEFORE any wire frame is emitted.
2. The reboot reads the radio's :class:`rebootIfRequired` OID
   (catalog v2 — see commit ``f85f2ae``). When the radio returns
   ``rebootNotRequired(0)`` the tool short-circuits with
   ``rebooted=False, reason='not_required_by_firmware'`` — the
   firmware's authoritative vote is the primary signal, NOT the
   table-driven band-crossing detector.
3. When ``rebootIfRequired`` returns ``rebootRequired(1)`` (or the
   OID read fails — fail-closed), the helper emits the SET on the
   ``reboot`` OID (``1.3.6.1.4.1.161.19.3.3.3.2.0``,
   ``whispBoxControls 2``). The SET values are documented as:
   * ``reboot(1)``   — normal reboot
   * ``fullReboot(2)`` — full reboot (450i only)
   Per the 25.x MIB DESCRIPTION: "Setting the variable to 1 will
   reboot the unit. When the unit finishes rebooting, it will be
   in finishedReboot state. Setting the variable to 2 will perform
   a full reboot of a 450i radio, while performing a normal reboot
   on other radios." The helper defaults to ``fullReboot(2)`` for
   450i hardware (the only model the registry ships today).
4. After the SET the helper does NOT poll for re-establishment —
   the orchestrator is responsible for re-running the pre-flight
   once the firmware reports the reboot complete. The
   ``POST_REBOOT`` intervention record carries the reboot metadata
   (timestamp, firmware vote, dry-run flag) so the audit trail is
   unambiguous.
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
(success, dry-run, or skipped) through
:func:`nora.intervention_writer.writer.save_intervention_record`.

Zero-Leakage: only TEST-NET-1 (``192.0.2.x``) host literals; no real
IPs, hostnames, serials, or credentials.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field

from nora.hitl.tokens import verify_approval_token
from nora.intervention_writer.writer import save_intervention_record

if TYPE_CHECKING:
    from nora.config import Settings


logger = logging.getLogger("nora.drivers.snmp_pmp450i.reboot")


# ---------------------------------------------------------------------------
# Reboot OID names + typed models — WU-C surface.
# ---------------------------------------------------------------------------


# The reboot OID name. Resolved at runtime via the catalog envelope.
# Per the 25.x MIB ``whispBoxControls 2``:
#   INTEGER { finishedReboot(0), reboot(1), fullReboot(2) }
REBOOT_OID_NAME: str = "reboot"

# The reboot-vote OID name. Per the 25.x MIB ``whispBoxControls 4``:
#   INTEGER { rebootNotRequired(0), rebootRequired(1) }
REBOOT_IF_REQUIRED_OID_NAME: str = "rebootIfRequired"

# SET values per the MIB DESCRIPTION.
_REBOOT_VALUE_NORMAL: int = 1
_REBOOT_VALUE_FULL: int = 2

# The reboot-vote result values.
_REBOOT_NOT_REQUIRED: int = 0
_REBOOT_REQUIRED: int = 1


class RebootResult(BaseModel):
    """Typed reboot tool result — WU-C read/write tool return.

    Fields:

    * ``device_id`` — the inventory device the reboot ran against.
    * ``rebooted`` — True when the SET was emitted (real path or
      dry-run); False when the firmware's ``rebootIfRequired``
      vote said no.
    * ``reason`` — ``"not_required_by_firmware"`` when the vote
      said no; ``"vote_unreadable"`` when the read failed and the
      helper default-fired (fail-closed); ``None`` on the
      normal path.
    * ``firmware_vote`` — verbatim integer from the
      ``rebootIfRequired`` OID (``0`` = not required,
      ``1`` = required). ``None`` when the read failed.
    * ``dry_run`` — ``True`` when the tool did NOT perform a write
      SET (production V2CClient path). Defaults to ``False``.
    * ``would_set`` — the SET pair the tool WOULD have emitted in
      dry-run mode. Empty on real reboots.
    * ``set_calls`` — the SET calls actually emitted on real reboots.
      Empty on dry-run mode.
    * ``expected_recovery_seconds`` — operator-facing hint (the
      Cambium 450i takes ~90-120 seconds to fully boot). The
      helper emits ``120`` as a static value; a future change can
      probe ``bootTime`` for accuracy.
    * ``hitl_required`` — always ``True`` (Tier-2 invariant). Kept
      on the result so the orchestrator can confirm without
      re-reading the spec.

    The ``dry_run`` / ``would_set`` contract seam mirrors the
    ``MigrationResult`` contract in :mod:`nora.drivers.snmp_pmp450i.migrate`.
    """

    model_config = ConfigDict(frozen=True)

    device_id: str
    rebooted: bool = False
    reason: str | None = None
    firmware_vote: int | None = None
    dry_run: bool = False
    would_set: list[tuple[str, str | int | float]] = Field(default_factory=list)
    set_calls: list[str] = Field(default_factory=list)
    expected_recovery_seconds: int = 120
    hitl_required: bool = True


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_int(value: str | int | None) -> int:
    """Coerce ``value`` to ``int``; missing values collapse to ``0``.

    Cambium agents return ``int`` for ``rebootIfRequired`` but the
    fold tolerates ``str`` (OctetString variants on legacy firmware)
    via ``int(value)``; unparseable strings collapse to ``0`` so the
    vote default-fires to "not required" (fail-safe).
    """
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _utc_now_iso() -> str:
    """Return the current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public helper — WU-C reboot tool body.
# ---------------------------------------------------------------------------


def fetch_reboot(
    *,
    driver: Any,
    device_id: str,
    approval_token: str | None,
    settings: "Settings | None" = None,
    force: bool = False,
) -> dict[str, Any]:
    """Run the HITL-gated reboot for ``device_id``.

    Args:
        driver: ``Pmp450iSnmpDriver`` instance (carries inventory +
            catalog + client factory).
        device_id: Inventory device id.
        approval_token: Operator-issued HITL token. The literal
            :class:`AutonomousMutationRejected` fires on missing /
            invalid tokens BEFORE any wire frame.
        settings: Optional :class:`Settings` instance (carries
            ``nora_hitl_signing_key``).
        force: When ``True``, skip the ``rebootIfRequired`` vote
            and emit the SET unconditionally. Default ``False`` —
            the firmware's vote is the authoritative signal.

    Returns:
        A dict matching the :class:`RebootResult` schema. On a
        firmware-vote "not required" the helper returns
        ``rebooted=False, reason='not_required_by_firmware'``
        without emitting any SET frame.

    Raises:
        AutonomousMutationRejected: missing or invalid approval
            token. The literal message is the contract seam.
        DeviceNotFoundError: unknown ``device_id``.
        CatalogNotFoundError: no catalog for the device's
            ``(vendor, model, firmware)`` triple.
        LookupError: the catalog entry does not carry the
            ``reboot`` or ``rebootIfRequired`` OID names.
    """
    # 1. HITL gate — fires FIRST. The signing key is sourced from
    # `Settings.nora_hitl_signing_key`; lazy fail-closed if empty.
    signing_key = getattr(settings, "nora_hitl_signing_key", None) if settings is not None else None
    verify_approval_token(approval_token, signing_key=signing_key)

    # 2. Resolve device + catalog.
    device = driver._inventory.get(device_id)  # noqa: SLF001 — internal API
    catalog = driver._catalog_registry.resolve(  # noqa: SLF001 — internal API
        (device.vendor, device.model, device.firmware)
    )

    # Reboot OID names MUST be in the catalog.
    reboot_oid_dotted = catalog.oids.get(REBOOT_OID_NAME)
    if reboot_oid_dotted is None:
        raise LookupError(
            f"reboot OID {REBOOT_OID_NAME!r} missing from catalog "
            f"(vendor={catalog.vendor}, model={catalog.model}, "
            f"firmware={catalog.firmware})"
        )
    reboot_if_required_dotted = catalog.oids.get(REBOOT_IF_REQUIRED_OID_NAME)
    if reboot_if_required_dotted is None:
        raise LookupError(
            f"rebootIfRequired OID {REBOOT_IF_REQUIRED_OID_NAME!r} missing "
            f"from catalog (vendor={catalog.vendor}, model={catalog.model}, "
            f"firmware={catalog.firmware})"
        )

    # 3. Read the firmware's reboot vote. Default fire on read
    # failure (fail-closed) — the operator can re-invoke with
    # ``force=True`` if the read is unreliable.
    # Issue #80: open the client via ``_writable_client_factory``
    # so the SET frames below actually reach the wire. The
    # read-only ``_client_factory`` would always fall through to the
    # dry-run seam because the produced ``V2CClient`` does NOT
    # implement ``WritableSnmpClient``. The
    # ``rebootIfRequired`` GET is read-only and works on any client.
    client = driver._writable_client_factory(device)  # noqa: SLF001 — internal API
    firmware_vote: int | None = None
    vote_unreadable = False
    try:
        try:
            raw_vote = client.get_oid(reboot_if_required_dotted)
            firmware_vote = _coerce_int(raw_vote)
        except Exception:  # noqa: BLE001 - all wire failures fail-closed
            vote_unreadable = True
            firmware_vote = None
    finally:
        try:
            client.close()
        except Exception:  # pragma: no cover - close is best-effort
            pass

    # 4. Decide whether to emit the SET.
    should_reboot = bool(force) or vote_unreadable or firmware_vote == _REBOOT_REQUIRED
    if not should_reboot and firmware_vote == _REBOOT_NOT_REQUIRED:
        # Firmware says no — return without emitting any SET frame.
        result = RebootResult(
            device_id=str(getattr(device, "host", device_id)),
            rebooted=False,
            reason="not_required_by_firmware",
            firmware_vote=firmware_vote,
            dry_run=False,
            would_set=[],
            set_calls=[],
        )
    else:
        # 5. Decide between real-SET and dry-run based on the
        # client's capabilities. Issue #80: the gate now checks
        # for the ``set`` verb (the ``WritableSnmpClient`` Protocol
        # contract) instead of the non-existent ``apply_oid``. The
        # production ``V2CClient`` exposes ``set`` only through the
        # ``WritableV2CClient`` adapter wired via
        # ``_writable_client_factory`` above.
        has_set = hasattr(client, "set")
        if has_set:
            set_calls: list[str] = []
            try:
                # Per the 25.x MIB: fullReboot(2) on 450i hardware
                # (the only Cambium model in scope today). A future
                # change can read the platform from ``platformType``
                # (whispBoxStatus 12) to dispatch normal vs full.
                # ``_REBOOT_VALUE_FULL`` is the Cambium MIB enum
                # value ``fullReboot(2)``; it is NOT a frequency, so
                # no kHz conversion applies (unlike
                # ``migration_oids['migrateCarrierFrequency']``).
                set_calls.append(f"{reboot_oid_dotted}={_REBOOT_VALUE_FULL}")
                client.set(reboot_oid_dotted, _REBOOT_VALUE_FULL)
            finally:
                try:
                    client.close()
                except Exception:  # pragma: no cover - close is best-effort
                    pass
            result = RebootResult(
                device_id=str(getattr(device, "host", device_id)),
                rebooted=True,
                reason=(
                    "vote_unreadable_fail_closed"
                    if vote_unreadable
                    else "reboot_required_by_firmware"
                ),
                firmware_vote=firmware_vote,
                dry_run=False,
                would_set=[],
                set_calls=set_calls,
            )
        else:
            # Dry-run fallback (issue #80) — same pattern as
            # ``MigrationResult``. The client lacks the ``set`` verb
            # (e.g. a thin read-only mock); no SET frame is emitted
            # but a typed ``dry_run=True`` result surfaces the
            # would-be SET pair so operators see what WOULD have
            # happened, instead of crashing with ``AttributeError``.
            would_set: list[tuple[str, str | int | float]] = [
                (reboot_oid_dotted, _REBOOT_VALUE_FULL)
            ]
            try:
                client.close()
            except Exception:  # pragma: no cover
                pass
            logger.info(
                "reboot: client lacks set; emulating SET %s=%s on device=%s",
                reboot_oid_dotted,
                _REBOOT_VALUE_FULL,
                device_id,
            )
            result = RebootResult(
                device_id=str(getattr(device, "host", device_id)),
                rebooted=True,
                reason=(
                    "vote_unreadable_fail_closed"
                    if vote_unreadable
                    else "reboot_required_by_firmware"
                ),
                firmware_vote=firmware_vote,
                dry_run=True,
                would_set=would_set,
                set_calls=[],
            )

    # 6. Emit exactly one ``save_intervention_record`` per completion.
    record_status = "DRY_RUN" if result.dry_run else ("COMPLETED" if result.rebooted else "SKIPPED")
    record_name_prefix = "[DRY-RUN] " if result.dry_run else ""
    findings_and_dictamen = (
        f"dry_run={result.dry_run}; rebooted={result.rebooted}; "
        f"reason={result.reason or 'n/a'}; "
        f"firmware_vote={result.firmware_vote}; "
        f"would_set={result.would_set!r}; set_calls={result.set_calls!r}"
    )
    try:
        if settings is None:
            raise RuntimeError("reboot.fetch_reboot requires an explicit Settings instance")
        save_intervention_record(
            settings,
            {
                "intervention_id": "INT-REBOOT-{ts}".format(ts=int(time.time())),
                "timestamp_iso": _utc_now_iso(),
                "timestamp_unix": int(time.time()),
                "ticket_number": "REBOOT-7400",
                "target_ip": str(getattr(device, "host", device_id)),
                "stage": "POST_REBOOT",
                "record_name": (f"{record_name_prefix}RF reboot of {device_id}"),
                "status": record_status,
                "agent_name": "nora-mcp",
                "findings_and_dictamen": findings_and_dictamen,
                "created_at": _utc_now_iso(),
                "network_equipment": {
                    "target_ip": str(getattr(device, "host", device_id)),
                    "carrier_frequency_mhz": None,  # not in scope; reboot is band-class-agnostic
                },
                "rebooted": result.rebooted,
                "reason": result.reason,
                "firmware_vote": result.firmware_vote,
                "dry_run": result.dry_run,
                "would_set": [list(item) for item in result.would_set],
            },
        )
    except Exception:  # pragma: no cover - writer has its own status codes
        logger.exception("reboot: save_intervention_record failed for device=%s", device_id)

    return result.model_dump(mode="json")


__all__ = [
    "RebootResult",
    "REBOOT_OID_NAME",
    "REBOOT_IF_REQUIRED_OID_NAME",
    "fetch_reboot",
]
