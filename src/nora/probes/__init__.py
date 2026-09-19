"""ICMP probe package — public surface for issue #61 sector stability probe.

This package is the *only* place under ``src/nora/`` allowed to import
:mod:`socket` (the repo-wide air-gap AST ban targets ``src/nora/drivers/``
and ``src/nora/prompts/``, not this directory). The Linux unprivileged
ICMP echo path uses ``socket.socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP)``
which requires the operator to put the running user's gid in
``net.ipv4.ping_group_range`` (see ``INSTALL.md``, PR3).
"""

from __future__ import annotations

from nora.probes.discovery import discover_targets
from nora.probes.exceptions import (
    IcmpEngineError,
    IcmpTimeoutError,
    IcmpUnreachableError,
)
from nora.probes.icmp import IcmpPinger, IcmpSample, UnprivilegedIcmpPinger
from nora.probes.models import DiscoveryResult, ProbeTarget, ProbeTargetRole

__all__ = [
    "DiscoveryResult",
    "IcmpEngineError",
    "IcmpPinger",
    "IcmpSample",
    "IcmpTimeoutError",
    "IcmpUnreachableError",
    "ProbeTarget",
    "ProbeTargetRole",
    "UnprivilegedIcmpPinger",
    "discover_targets",
]
