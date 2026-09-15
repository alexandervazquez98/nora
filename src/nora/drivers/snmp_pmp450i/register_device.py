"""`register_device` MCP tool — issue #42.

Builds a frozen `Device` via :func:`DeviceResolver.build`, optionally
issues a cheap ``sysDescr`` GET against the agent, and inserts the
device into :class:`MutableInventory` on success. Returns a typed
``DeviceRecord`` whose ``community`` field is a ``SecretStr`` so the
tool boundary masks it via ``model_dump(mode="json")``.

Error taxonomy:

* ``InvalidHostError``   — malformed host at the Pydantic boundary (pre-wire).
* ``DeviceUnreachable``  — wire-level failure (``OSError`` / ``TimeoutError``).
* ``InvalidCommunity``   — auth-rejected (``puresnmp.exc.SnmpError``).
* ``DuplicateDeviceError`` — duplicate ``device_id`` from the overlay.

On ANY failure path, NO row is inserted into the mutable inventory.
The wired ``@mcp.tool`` wrapper at ``server.py`` calls
``_register_device_impl`` and converts the typed exception into a
serialisable MCP error.

The implementation accepts an injectable ``client_factory`` so tests
can stub the wire layer without monkey-patching ``SnmpClient``. The
factory receives the resolved ``Device`` and returns an object that
satisfies the :class:`SnmpClient` Protocol (``get_oid`` / ``walk`` /
``close``).
"""

from __future__ import annotations

import ipaddress
from typing import Any, Callable

import puresnmp.exc
from pydantic import BaseModel, ConfigDict, SecretStr

from nora.drivers.exceptions import (
    DeviceUnreachable,
    DuplicateDeviceError,
    InvalidCommunity,
    InvalidHostError,
)
from nora.drivers.inventory import Device, SnmpVersion
from nora.drivers.mutable_inventory import MutableInventory
from nora.drivers.resolver import DeviceResolver, SnmpCredentials
from nora.drivers.snmp_pmp450i.client import SnmpClient

# RFC 1213 sysDescr — the agent's human-readable identification string.
# Same OID `Pmp450iSnmpDriver.report_firmware` reads; the validate
# path uses it as the cheapest reachability probe.
_SYSDESCR_OID: str = "1.3.6.1.2.1.1.1.0"


class DeviceRecord(BaseModel):
    """Typed return shape for ``register_device``.

    `community` is a `SecretStr` so `model_dump(mode="json")` masks it
    to ``"**********"`` at the tool boundary — Zero-Leakage contract
    (§1 of the orchestrator prompt). `validated` carries the
    wire-validation outcome so the caller can distinguish an inserted
    device that has been SNMP-reachable from one registered in
    ad-hoc mode (``validate=False``).
    """

    model_config = ConfigDict(frozen=True)

    device_id: str
    host: str
    vendor: LiteralAdapter  # see below
    model: LiteralAdapter  # see below
    firmware: str
    snmp_version: SnmpVersion
    community: SecretStr
    validated: bool


# Local type alias — kept private so the public surface only re-exports
# ``DeviceRecord``. ``LiteralAdapter`` is ``str`` at runtime; the
# alias is a no-op for callers but documents intent in the type checker.
LiteralAdapter = str


def _validate_ipv4_literal(host: str) -> None:
    """Raise ``InvalidHostError`` if ``host`` is not an RFC-5737-compatible IPv4 literal.

    The contract is intentionally narrow — operators paste an IPv4 in
    chat and the tool accepts the literal form only. DNS names are out
    of scope (issue #42 spec: "register the host the operator gave
    us"); a future change can widen the validator without breaking
    callers.
    """
    try:
        ipaddress.IPv4Address(host)
    except (ipaddress.AddressValueError, ValueError) as exc:
        raise InvalidHostError(host) from exc


def _build_device(host: str, community: str) -> Device:
    """Run the pre-wire validation + device construction path.

    Raises ``InvalidHostError`` on a malformed host, OR a
    ``ValueError`` if the resolved ``Device`` model validator rejects
    the credentials (e.g. an empty community). The returned ``Device``
    is frozen Pydantic and ready to insert.
    """
    _validate_ipv4_literal(host)
    creds = SnmpCredentials(community=community)  # type: ignore[arg-type]
    return DeviceResolver.build(host, "v2c", creds)


def _device_record(device: Device, *, validated: bool) -> DeviceRecord:
    """Map a frozen ``Device`` onto the typed ``DeviceRecord``.

    The ``community`` field is a ``SecretStr`` so the wire response
    masks it via ``model_dump(mode="json")``. Empty / v3 creds raise
    ``ValidationError`` (caught at the tool boundary) — the surface
    is v2c-only per the issue spec.
    """
    community = device.community
    if community is None:  # pragma: no cover — v2c path always has community
        raise ValueError(f"Device {device.device_id!r}: v2c requires a non-empty community")
    return DeviceRecord(
        device_id=device.device_id,
        host=device.host,
        vendor=device.vendor,
        model=device.model,
        firmware=device.firmware,
        snmp_version=device.snmp_version,
        community=community,
        validated=validated,
    )


def _validate_sysdescr(client: SnmpClient) -> str:
    """Issue the cheap sysDescr GET and translate wire errors to typed driver errors.

    Maps:
    * ``OSError`` / ``TimeoutError`` -> ``DeviceUnreachable(host)``
    * ``puresnmp.exc.SnmpError``     -> ``InvalidCommunity(community)`` (auth rejected)
    * Other                          -> propagates (unexpected — wired caller maps)
    """
    try:
        raw = client.get_oid(_SYSDESCR_OID)
    except (OSError, TimeoutError) as exc:
        # The wire error doesn't always carry a useful host (e.g. a
        # raw TimeoutError from the socket layer); the caller threads
        # the host through `DeviceUnreachable(host)` separately.
        raise DeviceUnreachable("<unknown>") from exc
    except puresnmp.exc.SnmpError as exc:
        # Treat any puresnmp SnmpError as auth-rejected for v2c — the
        # wire response when the community is wrong IS a SnmpError.
        raise InvalidCommunity("<unknown>") from exc
    return str(raw)


def _register_device_impl(
    *,
    driver: Any,
    host: str,
    community: str,
    validate: bool,
    sanitizer: Any | None,
    mutable_inventory: MutableInventory,
    client_factory: Callable[[Device], SnmpClient] | None = None,
) -> DeviceRecord:
    """Build + validate + insert a runtime device.

    Parameters
    ----------
    driver
        The driver singleton (reserved for future telemetry hooks;
        unused by the v1 validate path). Pass ``None`` for tests.
    host
        IPv4 literal the operator pasted in chat. Validated pre-wire
        against :func:`ipaddress.IPv4Address`.
    community
        v2c community string. Wrapped in ``SecretStr`` at the
        ``DeviceRecord`` boundary so the wire response masks it.
    validate
        When ``True`` issue a sysDescr GET against OID
        ``1.3.6.1.2.1.1.1.0`` BEFORE inserting. Failure raises a
        typed exception and inserts nothing.
    sanitizer
        Optional `Sanitizer` instance. Held as a seam for the tool
        wrapper at ``server.py`` so the upper layer can pass
        ``_sanitizer`` and the helper can use it for free-text field
        sanitisation. v1 accepts but does not require it.
    mutable_inventory
        The wrapper that holds runtime-registered devices. Inserted
        ONLY on the success path — every failure mode leaves the
        overlay unchanged.
    client_factory
        Callable that returns an :class:`SnmpClient` for the resolved
        ``Device``. Tests inject fakes here; production passes the
        driver's ``client_factory``. Defaults to
        :func:`default_client_factory` when not supplied.

    Returns
    -------
    DeviceRecord
        Typed record with ``community`` masked at the Pydantic
        boundary. Callers serialise via ``.model_dump(mode="json")``.

    Raises
    ------
    InvalidHostError
        ``host`` is not an IPv4 literal (pre-wire).
    DeviceUnreachable
        ``validate=True`` and the wire GET raised ``OSError`` or
        ``TimeoutError``. No insert.
    InvalidCommunity
        ``validate=True`` and the wire GET raised
        ``puresnmp.exc.SnmpError``. No insert.
    DuplicateDeviceError
        The resolved ``device_id`` collides with an existing overlay
        entry. (The collision is statistically negligible because
        ``DeviceResolver.build`` uses ``secrets.token_hex(3)``, but
        the typed exception surfaces it for completeness.)
    """
    del driver  # reserved for future telemetry hooks

    device = _build_device(host, community)

    if validate:
        if client_factory is None:
            from nora.drivers.snmp_pmp450i.driver import default_client_factory

            client_factory = default_client_factory
        client = client_factory(device)
        try:
            try:
                _validate_sysdescr(client)
            except DeviceUnreachable as exc:
                # Re-raise with the actual host the operator typed.
                raise DeviceUnreachable(host) from exc
            except InvalidCommunity as exc:
                raise InvalidCommunity(community) from exc
        finally:
            try:
                client.close()
            except Exception:  # pragma: no cover - close is best-effort
                pass

    try:
        mutable_inventory.register(device)
    except DuplicateDeviceError:
        # `DeviceResolver.build` uses `secrets.token_hex(3)` so the
        # collision probability is ~2^-24 per call; the wrapper
        # surfaces the typed error so the operator sees it loudly
        # rather than silently overwriting.
        raise

    return _device_record(device, validated=validate)


__all__ = ["DeviceRecord", "_register_device_impl"]
