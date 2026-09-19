"""`SnmpClient` Protocol — read-only surface.

The driver layer depends on this Protocol; the only operations exposed
are `get_oid` (one GET per OID), `walk` (subtree, read-only), and
`close` (release the transport). Any write verb is intentionally
absent — the public API guarantee that no caller can mutate a device
through this Protocol.

The concrete implementations (`V2CClient`, `V3Client`) wrap
`puresnmp.PyWrapper` so callers receive native Python types (`str`,
`int`) rather than `x690` types.

Driver-R2 carve-out (issue #62, 2026-09-19): the real Cambium sweep
protocol requires SET frames (write duration + arm + start) against
the WHISP-BOX-MIBV2-MIB sweep scalars. The base `SnmpClient` Protocol
stays read-only at the type level; the write capability lives on the
opt-in :class:`WritableSnmpClient` Protocol below. Callers that need
to emit SET frames import `WritableSnmpClient` explicitly and wire a
`Writable*` adapter through the `writable_client_factory` constructor
parameter on the driver. The carve-out is enforced by the property
test in `tests/test_driver_snmp450i_readonly.py::test_snmp_client_protocol_exposes_no_write_verbs`
which inspects `vars(SnmpClient)` directly; `set` MUST NOT appear on
the base Protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SnmpClient(Protocol):
    """Read-only SNMP client surface.

    NOTE: NO `set`, `update`, `setbulk`, `bulk_set`, or `write` methods
    are present. Adding one is a Driver-R2 violation and is caught by
    `tests/test_driver_snmp450i_readonly.py` (the property test
    over `dir(SnmpClient)`).
    """

    def get_oid(self, oid: str) -> str | int:
        """Fetch the value at dotted `oid`.

        Returns the underlying scalar (`str` for OctetString, `int`
        for Integer / Counter / Gauge). The driver raises a typed
        exception on wire failures.
        """
        ...

    def walk(self, base_oid: str) -> list[tuple[str, str | int]]:
        """Subtree walk under `base_oid` — returns `(oid, value)` pairs."""
        ...

    def close(self) -> None:
        """Release the underlying transport."""
        ...


@runtime_checkable
class WritableSnmpClient(SnmpClient, Protocol):
    """Opt-in write-capable SNMP client surface (Driver-R2 carve-out).

    Issue #62 (2026-09-19): the Cambium WHISP-BOX-MIBV2-MIB sweep
    protocol requires three SET frames against the spectrum-scan
    scalars (`.220.0` duration, `.221.0` arm, `.221.0` start). The
    base :class:`SnmpClient` Protocol explicitly forbids write verbs;
    this Protocol extends it with one (and only one) `set` verb.

    Reviewers MUST keep this surface narrow. Adding `update`,
    `setbulk`, `bulk_set`, or `write` to this Protocol is a Driver-R2
    violation; the allow-list in
    `tests/test_driver_snmp450i_readonly.py::_WRITABLE_SEAM_FILES`
    enumerates the files that may legitimately contain a `set`
    identifier, and every other driver file still asserts zero write
    identifiers via AST scan.

    Implementations MUST be a thin adapter over a read-only client
    (the `Writable*` adapters in `v2c.py` / `v3.py` are the canonical
    pattern): they wrap an existing read-only client and forward
    `get_oid` / `walk` / `close` to it, exposing `set` only via the
    `puresnmp.PyWrapper.set` async coroutine.
    """

    def set(self, oid: str, value: str | int) -> None:
        """Emit one SNMP SET frame against `oid` with the given `value`.

        The wire-level semantics follow :class:`puresnmp.PyWrapper.set`:
        `value` is encoded as the appropriate ASN.1 type (Integer for
        `int`, OCTET STRING for `str`). Typed driver exceptions
        (`SnmpTimeoutError`, `NetworkUnreachableError`) propagate from
        the underlying client on wire failure.

        Returns ``None``. Callers that need to observe the SET
        response for parity with `puresnmp.PyWrapper.set` should reach
        into the underlying transport directly — the Protocol surface
        intentionally returns nothing so the call sites in
        `spectrum.py` / `migrate.py` stay declarative.
        """
        ...


__all__ = ["SnmpClient", "WritableSnmpClient"]
