"""Typed exception hierarchy for the ICMP probe engine.

These exceptions are the *only* failure mode callers of
:meth:`nora.probes.icmp.IcmpPinger.ping` should expect. They live at the
`src.nora.probes` package boundary so the discovery helper (WU-1.2),
coordinator (WU-1.3), and FastMCP tool wiring (WU-1.5) can catch them
precisely without parsing string messages.
"""

from __future__ import annotations


class IcmpEngineError(Exception):
    """Root typed exception for the ICMP probe engine."""


class IcmpTimeoutError(IcmpEngineError):
    """Per-packet ICMP echo timed out (no echo reply within `timeout` seconds)."""


class IcmpUnreachableError(IcmpEngineError):
    """ICMP destination unreachable or socket cannot be created at all."""
