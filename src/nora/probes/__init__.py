"""ICMP probe package — public surface for issue #61 sector stability probe.

This package is the *only* place under ``src/nora/`` allowed to import
:mod:`socket` (the repo-wide air-gap AST ban targets ``src/nora/drivers/``
and ``src/nora/prompts/``, not this directory). The Linux unprivileged
ICMP echo path uses ``socket.socket(AF_INET, SOCK_DGRAM, IPPROTO_ICMP)``
which requires the operator to put the running user's gid in
``net.ipv4.ping_group_range`` (see ``INSTALL.md``, PR3).

PR2 (issue #61) extends the surface with the post-run aggregation
layer — per-node ``NodeMetrics``, sector-level ``SectorDelta``,
``DiagnosticVerdict`` (8-state matrix), and the atomic JSON
persistence helper (``save_probe_run``). The package-level
``__all__`` mirrors the documented public surface verbatim; sub-
modules add their own ``__all__`` for downstream imports.
"""

from __future__ import annotations

from nora.probes.diagnostic import (
    DiagnosticVerdict,
    PerSmVerdict,
    SectorVerdict,
    classify_node,
    classify_sector,
)
from nora.probes.discovery import discover_targets
from nora.probes.exceptions import (
    IcmpEngineError,
    IcmpTimeoutError,
    IcmpUnreachableError,
)
from nora.probes.icmp import IcmpPinger, IcmpSample, UnprivilegedIcmpPinger
from nora.probes.metrics import NodeMetrics, compute_metrics
from nora.probes.models import (
    DiscoveryResult,
    ProbeRunCompleted,
    ProbeRunRecord,
    ProbeRunSettings,
    ProbeRunStarted,
    ProbeRunSummary,
    ProbeTarget,
    ProbeTargetRole,
)
from nora.probes.persistence import (
    INVALID_INPUT,
    INVALID_PAYLOAD,
    OK,
    PATH_TRAVERSAL_DETECTED,
    WRITE_ERROR,
    ProbeRunPayload,
    build_probe_filename,
    save_probe_run,
)
from nora.probes.probe import ProbeConfigurationError, generate_run_id, run_probe
from nora.probes.sector_delta import SectorDelta, compute_sector_delta
from nora.probes.state import ProbeRunRegistry, RunState, RunStatus, start_probe_run

__all__ = [
    "DiscoveryResult",
    "DiagnosticVerdict",
    "INVALID_INPUT",
    "INVALID_PAYLOAD",
    "IcmpEngineError",
    "IcmpPinger",
    "IcmpSample",
    "IcmpTimeoutError",
    "IcmpUnreachableError",
    "NodeMetrics",
    "OK",
    "PATH_TRAVERSAL_DETECTED",
    "PerSmVerdict",
    "ProbeConfigurationError",
    "ProbeRunCompleted",
    "ProbeRunPayload",
    "ProbeRunRecord",
    "ProbeRunRegistry",
    "ProbeRunSettings",
    "ProbeRunStarted",
    "ProbeRunSummary",
    "ProbeTarget",
    "ProbeTargetRole",
    "RunState",
    "RunStatus",
    "SectorDelta",
    "SectorVerdict",
    "UnprivilegedIcmpPinger",
    "WRITE_ERROR",
    "build_probe_filename",
    "classify_node",
    "classify_sector",
    "compute_metrics",
    "compute_sector_delta",
    "discover_targets",
    "generate_run_id",
    "run_probe",
    "save_probe_run",
    "start_probe_run",
]
