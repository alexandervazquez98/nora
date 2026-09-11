"""`SnmpClient` Protocol — read-only surface.

The driver layer depends on this Protocol; the only operations exposed
are `get_oid` (one GET per OID), `walk` (subtree, read-only), and
`close` (release the transport). Any write verb is intentionally
absent — the public API guarantee that no caller can mutate a device
through this Protocol.

The concrete implementations (`V2CClient`, `V3Client`) wrap
`puresnmp.PyWrapper` so callers receive native Python types (`str`,
`int`) rather than `x690` types.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SnmpClient(Protocol):
    """Read-only SNMP client surface.

    NOTE: NO `set`, `update`, `setbulk`, `bulk_set`, or `write` methods
    are present. Adding one is a Driver-R2 violation and is caught by
    `tests/test_driver_snmp_pmp450i_readonly.py` (the property test
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


__all__ = ["SnmpClient"]
