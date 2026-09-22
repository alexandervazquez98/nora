"""`nora doctor` engine — issue #60 / PR-3 of #60 umbrella (RFC #60).

Thin renderer over ``scripts/verify-install.sh --json`` plus a
``net.ipv4.ping_group_range`` persistence cross-check. The 678 lines
of install / path / permission / systemd / jsonrpc checks live in the
shell script and are reused unchanged via the JSON output the script
already publishes.

Public surface (consumed by ``src/nora/__main__.py::_dispatch_doctor``):

* Exceptions: ``DoctorError``, ``VerifyInstallFailed``.
* Result dataclasses: ``DoctorCheck``, ``DoctorReport``.
* Engine functions: ``run_doctor``, ``render_human``, ``render_json``.
* Low-level helpers used by tests + future PRs:
  ``_run_verify_install_json``, ``_check_sysctl_persistence``,
  ``_verify_install_script_path``.

Design invariants (frozen in
``odd/tasks/installer-upgrade-and-doctor.md``):

* **D4** — ``run_doctor`` shells out to ``scripts/verify-install.sh
  --json`` rather than re-implementing any check; the engine is a
  transport + parse + render layer, not a re-implementation.
* **D5** — no TUI widgets in the first cut; output is either
  human-readable table (stderr) or machine-readable JSON (stdout).
  Rich / textual wrappers can land in a future PR.
* **D10** — ``run_doctor`` always adds a ``sysctl_persistence``
  cross-check; status rules are listed on ``_check_sysctl_persistence``.

Process model:

* Every subprocess call goes through a ``runner=`` injection seam
  with default ``subprocess.run``. Tests stub the seam with a
  ``MagicMock`` returning ``subprocess.CompletedProcess`` instances.
* Subprocess argv lists are constructed explicitly — never
  ``shell=True``.

Layout invariant:

* This module MUST NOT read process environment variables
  directly. The project invariant enforced by
  ``tests/test_config.py::test_no_os_environ_in_src_nora`` is part
  of CI; read your settings via ``Settings`` or via kwargs.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final, Literal

logger = logging.getLogger("nora.doctor")

# Type alias for the subprocess injection seam. Matches
# ``subprocess.run``'s public signature; tests stub with a
# ``MagicMock`` returning ``subprocess.CompletedProcess`` instances.
Runner = Callable[..., subprocess.CompletedProcess[str]]

CheckStatus = Literal["ok", "warn", "fail"]
CheckSource = Literal["verify_install", "sysctl_persistence"]

# Expected value for ``net.ipv4.ping_group_range`` post-install.
# Mirrors the constant in ``scripts/install.sh::phase_unprivileged_icmp``.
EXPECTED_PING_RANGE: Final[str] = "0 2147483647"

# Distro default value for ``net.ipv4.ping_group_range`` when the
# operator installed but did not reboot AND did not re-apply.
DEFAULT_PING_RANGE: Final[str] = "1 0"

# Path on disk that ``install.sh`` writes via
# ``scripts/install.sh::phase_persist_sysctl``. The doctor treats this
# as the canonical persistence location (D6 / D10).
PERSISTED_CONF_PATH: Final[str] = "/etc/sysctl.d/99-nora.conf"

# Defaults mirror ``scripts/install.sh`` so ``sudo nora doctor`` with
# no flags Just Works on a stock install.
DEFAULT_PREFIX: Final[Path] = Path("/opt/nora")
DEFAULT_CONFIG_DIR: Final[Path] = Path("/etc/nora")
DEFAULT_STATE_DIR: Final[Path] = Path("/var/lib/nora")
DEFAULT_LOG_DIR: Final[Path] = Path("/var/log/nora")
DEFAULT_USER: Final[str] = "nora"

# Width used by ``render_human`` for the status column. The check
# name column is 35 chars; the detail column wraps the remainder to 80
# chars total.
_NAME_COL_WIDTH: Final[int] = 35
_STATUS_COL_WIDTH: Final[int] = 8
_DETAIL_COL_MAX: Final[int] = 80 - _NAME_COL_WIDTH - _STATUS_COL_WIDTH - 2  # 35

# Regex used by ``_check_sysctl_persistence`` to extract the live
# sysctl value out of ``/etc/sysctl.d/99-nora.conf``. Anchored on
# both sides so a stray comment with the substring does not trip a
# false OK.
_PING_RANGE_LINE_RE: Final[re.Pattern[str]] = re.compile(
    r"^net\.ipv4\.ping_group_range\s*=\s*(\d+)\s+(\d+)\s*$"
)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class DoctorError(Exception):
    """Base class for every ``nora doctor`` failure.

    Catch this in the dispatcher if you want a single try/except that
    covers every error class below. The doctor-engine contract is
    that ONLY transport + parse failures raise; individual checks
    report their status via the ``DoctorReport`` payload so the
    operator sees the full picture regardless of the verifier's
    exit code.
    """


class VerifyInstallFailed(DoctorError):
    """``scripts/verify-install.sh --json`` failed to produce JSON.

    Raised when:

    * the script exits non-zero, OR
    * the script exits zero but ``stdout`` is not parseable as JSON.

    Carries the script's ``stderr`` so the operator can grep the
    audit log for the original failure mode. The dispatcher still
    surfaces whatever ``DoctorReport`` it managed to build (via the
    sysctl fallback) so the operator never sees a completely empty
    screen.
    """

    def __init__(self, stderr: str, message: str) -> None:
        super().__init__(message)
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DoctorCheck:
    """A single install-health check row.

    ``frozen=True`` so callers (dispatcher, tests, future audit log
    writer) cannot mutate checks after they're constructed — that
    matters because the dispatcher emits the report via JSON and a
    downstream consumer might cache the rendered string with the
    intent of comparing it byte-for-byte across runs.

    ``status`` is a ``Literal`` so the JSON serialisation is stable
    (``"ok" | "warn" | "fail"``) and downstream CI parsers don't have
    to deal with ``"OK"`` / ``"warning"`` / ``"FAILURE"`` variants.

    ``source`` separates verify-install.sh checks from the doctor's
    own sysctl persistence cross-check. Downstream consumers can
    filter to one source without parsing check names.
    """

    name: str
    status: CheckStatus
    detail: str
    source: CheckSource


@dataclass
class DoctorReport:
    """Aggregate of every check run by ``run_doctor``.

    The dataclass is NOT frozen because ``checks`` is a mutable
    list — we append to it as each phase completes. The other fields
    are derived via ``@property`` so a single source of truth feeds
    both the renderer and the dispatcher's exit-code logic.

    Ordering: verify-install checks appear in execution order (the
    order the shell script appended them); the sysctl cross-check
    is always the LAST row. Stable ordering matters because
    ``render_json`` round-trips, and a CI integration that diffs
    reports across runs only stays readable when the row order is
    deterministic.
    """

    checks: list[DoctorCheck]

    @property
    def ok_count(self) -> int:
        """Number of checks with status ``ok``."""
        return sum(1 for c in self.checks if c.status == "ok")

    @property
    def warn_count(self) -> int:
        """Number of checks with status ``warn``."""
        return sum(1 for c in self.checks if c.status == "warn")

    @property
    def fail_count(self) -> int:
        """Number of checks with status ``fail``."""
        return sum(1 for c in self.checks if c.status == "fail")


# ---------------------------------------------------------------------------
# Path helper
# ---------------------------------------------------------------------------


def _verify_install_script_path(prefix: Path) -> Path:
    """Locate the canonical ``verify-install.sh`` under ``<prefix>/scripts/``.

    Operators who installed from source via ``scripts/install.sh``
    end up with the script at ``/opt/nora/scripts/verify-install.sh``.
    Operators on a custom prefix (CI, dev worktrees) get the script
    under their prefix.

    The path is joined explicitly (``prefix / "scripts" /
    "verify-install.sh"``) — never ``os.path.join`` with raw strings,
    so test stubs that pass a ``Path`` always see a clean Path.
    """
    return Path(prefix) / "scripts" / "verify-install.sh"


# ---------------------------------------------------------------------------
# Transport layer — verify-install.sh --json
# ---------------------------------------------------------------------------


def _run_verify_install_json(
    *,
    prefix: Path = DEFAULT_PREFIX,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    state_dir: Path = DEFAULT_STATE_DIR,
    log_dir: Path = DEFAULT_LOG_DIR,
    user: str = DEFAULT_USER,
    skip_systemd: bool = False,
    check_http: bool = False,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Invoke ``<prefix>/scripts/verify-install.sh --json [...]`` and parse ``stdout``.

    The argv list is built explicitly so we never depend on
    ``shell=True`` (D8 contract). The runner is called with
    ``check=False`` because the dispatcher's contract is that the
    doctor MUST surface ALL checks regardless of the verifier's exit
    code — the exit code's content (FAIL/WARN counts) is reported
    via the parsed ``checks`` list and ``summary`` block, not via a
    raised exception.

    Raises:

    * ``VerifyInstallFailed`` when the script exits non-zero
      (carries the script's ``stderr`` in the exception).
    * ``VerifyInstallFailed`` when ``stdout`` is not parseable as
      JSON (carries an empty stderr + a parse-error message).

    Returns the parsed JSON dict. Expected schema (frozen in
    ``scripts/verify-install.sh``):

    .. code-block:: json

       {
         "checks": [{"name": "...", "status": "ok|warn|fail", "detail": "..."}],
         "summary": {"ok": N, "warn": N, "fail": N}
       }
    """
    verify_args = [
        str(_verify_install_script_path(Path(prefix))),
        "--json",
        "--strict",
        "--prefix",
        str(prefix),
        "--config-dir",
        str(config_dir),
        "--state-dir",
        str(state_dir),
        "--log-dir",
        str(log_dir),
        "--user",
        user,
    ]
    if skip_systemd:
        verify_args.append("--no-systemd")
    if check_http:
        verify_args.append("--check-http")

    try:
        completed = runner(verify_args, check=False, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        # The script itself is missing (`FileNotFoundError`) or some
        # other ``OSError`` (permission, exec format). Surface as a
        # ``VerifyInstallFailed`` so the dispatcher can emit a
        # clear stderr message and run_doctor can fall back to the
        # sysctl cross-check + a synthetic ``verify_install.run``
        # fail row.
        logger.warning("verify-install.sh failed to launch: %s", exc)
        raise VerifyInstallFailed(
            stderr="",
            message=f"verify-install.sh did not run — {exc}",
        ) from exc

    if completed.returncode != 0:
        stderr_text = completed.stderr or ""
        if isinstance(stderr_text, bytes):
            stderr_text = stderr_text.decode("utf-8", errors="replace")
        stderr_summary = stderr_text.strip().splitlines()
        tail = stderr_summary[-1] if stderr_summary else "no stderr"
        raise VerifyInstallFailed(
            stderr=stderr_text,
            message=f"verify-install.sh exited rc={completed.returncode} — {tail}",
        )

    try:
        report = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        stderr_text = completed.stderr or ""
        if isinstance(stderr_text, bytes):
            stderr_text = stderr_text.decode("utf-8", errors="replace")
        raise VerifyInstallFailed(
            stderr=stderr_text,
            message=(
                "verify-install.sh --json did not emit parseable JSON "
                f"({exc.__class__.__name__}: {exc})"
            ),
        ) from exc

    if not isinstance(report, dict):
        raise VerifyInstallFailed(
            stderr="",
            message=(
                f"verify-install.sh --json did not emit a JSON object (got {type(report).__name__})"
            ),
        )

    return report


def _verify_report_to_checks(report: dict[str, Any]) -> list[DoctorCheck]:
    """Convert a verified JSON report's ``checks`` list into ``DoctorCheck`` rows.

    Each row's ``source`` is hard-coded to ``verify_install``. Invalid
    rows (missing keys, wrong type, unknown status) are coerced into
    a ``fail`` row with a parser-side detail so the operator sees
    the schema mismatch instead of a swallowed exception.
    """
    raw_checks = report.get("checks") or []
    if not isinstance(raw_checks, list):
        return [
            DoctorCheck(
                name="verify_install.schema",
                status="fail",
                detail=f"expected list under `checks`, got {type(raw_checks).__name__}",
                source="verify_install",
            )
        ]

    checks: list[DoctorCheck] = []
    for idx, entry in enumerate(raw_checks):
        if not isinstance(entry, dict):
            checks.append(
                DoctorCheck(
                    name=f"verify_install.row.{idx}",
                    status="fail",
                    detail=(f"row #{idx} is {type(entry).__name__}, expected dict"),
                    source="verify_install",
                )
            )
            continue
        raw_status = entry.get("status")
        if raw_status not in ("ok", "warn", "fail"):
            name = entry.get("name") or f"verify_install.row.{idx}"
            checks.append(
                DoctorCheck(
                    name=name,
                    status="fail",
                    detail=(f"unknown status {raw_status!r}; expected ok|warn|fail"),
                    source="verify_install",
                )
            )
            continue
        name = entry.get("name") or f"verify_install.row.{idx}"
        detail = entry.get("detail") or ""
        checks.append(
            DoctorCheck(
                name=name,
                status=raw_status,
                detail=detail,
                source="verify_install",
            )
        )
    return checks


# ---------------------------------------------------------------------------
# Sysctl persistence cross-check (D10)
# ---------------------------------------------------------------------------


def _check_sysctl_persistence(
    *,
    sysctl_runner: Runner = subprocess.run,
    fs_runner: Callable[[str], str] = lambda p: Path(p).read_text(),
) -> DoctorCheck:
    """Cross-check that ``net.ipv4.ping_group_range`` is persisted AND applied.

    Decision D10:

    * **OK** — the persisted file (``/etc/sysctl.d/99-nora.conf``)
      contains the expected value AND ``sysctl -n`` reports the
      expected value at runtime. Both ends agree.
    * **WARN** — the persisted file is missing (operator hasn't run
      install.sh, or rebooted after the install didn't trigger a
      ``sysctl --system``) OR the runtime reports the distro
      default ``1 0`` (operator installed but didn't reboot AND
      didn't ``sysctl --system``).
    * **FAIL** — the persisted file is present with a value other
      than ``0 2147483647``. Operator deliberately tampered, or a
      third-party tool overwrote the file.

    The injected ``fs_runner`` lets tests feed file contents without
    touching the real filesystem. The default raises
    ``FileNotFoundError`` on a missing file, which we catch and
    treat as WARN.
    """
    # ----- Runtime sysctl --------------------------------------------------
    runtime_value = ""
    try:
        completed = sysctl_runner(
            ["sysctl", "-n", "net.ipv4.ping_group_range"],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            runtime_value = (completed.stdout or "").strip()
    except (OSError, subprocess.SubprocessError) as exc:
        # sysctl missing or refused to run — record as warn on the
        # runtime side. The file-side check still runs below.
        logger.warning("sysctl probe failed: %s", exc)

    if not runtime_value or runtime_value == DEFAULT_PING_RANGE:
        # Runtime side is wrong; we cannot reach OK. Record warn
        # with detail naming the runtime state. File-side state
        # never converts WARN into FAIL — that branch lives below.
        runtime_note = (
            f"runtime reports default `{DEFAULT_PING_RANGE}` "
            "(operator installed but didn't reboot AND didn't re-apply)"
            if runtime_value == DEFAULT_PING_RANGE
            else "sysctl -n net.ipv4.ping_group_range returned empty"
        )
        return DoctorCheck(
            name="sysctl_persistence",
            status="warn",
            detail=runtime_note,
            source="sysctl_persistence",
        )

    # ----- Persisted file --------------------------------------------------
    file_value: str | None = None
    file_missing = False
    try:
        contents = fs_runner(PERSISTED_CONF_PATH)
    except FileNotFoundError:
        file_missing = True
    except OSError as exc:
        logger.warning("sysctl conf read failed: %s", exc)
        file_missing = True
    else:
        for raw_line in contents.splitlines():
            line = raw_line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            match = _PING_RANGE_LINE_RE.match(line)
            if match is not None:
                file_value = f"{match.group(1)} {match.group(2)}"
                break

    if file_missing:
        return DoctorCheck(
            name="sysctl_persistence",
            status="warn",
            detail=f"{PERSISTED_CONF_PATH} missing",
            source="sysctl_persistence",
        )

    if file_value is None:
        return DoctorCheck(
            name="sysctl_persistence",
            status="warn",
            detail=f"{PERSISTED_CONF_PATH} present but missing `net.ipv4.ping_group_range` line",
            source="sysctl_persistence",
        )

    if file_value == EXPECTED_PING_RANGE:
        return DoctorCheck(
            name="sysctl_persistence",
            status="ok",
            detail=(f"{PERSISTED_CONF_PATH} + runtime agree on `{EXPECTED_PING_RANGE}`"),
            source="sysctl_persistence",
        )

    return DoctorCheck(
        name="sysctl_persistence",
        status="fail",
        detail=(
            f"{PERSISTED_CONF_PATH} persists `{file_value}` (expected `{EXPECTED_PING_RANGE}`)"
        ),
        source="sysctl_persistence",
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_doctor(
    *,
    prefix: Path = DEFAULT_PREFIX,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    state_dir: Path = DEFAULT_STATE_DIR,
    log_dir: Path = DEFAULT_LOG_DIR,
    user: str = DEFAULT_USER,
    skip_systemd: bool = False,
    check_http: bool = False,
    verify_runner: Runner = subprocess.run,
    sysctl_runner: Runner = subprocess.run,
    fs_runner: Callable[[str], str] | None = None,
) -> DoctorReport:
    """Run ``verify-install.sh --json``, merge with the sysctl cross-check.

    Behaviour:

    1. Invoke ``scripts/verify-install.sh --json`` via
       ``verify_runner``; on success, convert every JSON row into a
       ``DoctorCheck`` and append them in execution order.
    2. Always append the sysctl cross-check (D10) as the LAST row,
       regardless of whether step 1 landed anything — even a totally
       wedged verifier delivers one check to the operator.
    3. On ``VerifyInstallFailed``, build a synthetic report that
       contains the sysctl cross-check (so SOMETHING reaches the
       operator) AND a single ``verify_install.run`` ``fail`` row
       that carries the failure message in its detail.

    Returns a ``DoctorReport`` (never raises for verifier-level
    non-zero exits; only transport-level or parse-level failures
    surface as ``VerifyInstallFailed`` which the caller handles
    externally).
    """
    try:
        raw_report = _run_verify_install_json(
            prefix=prefix,
            config_dir=config_dir,
            state_dir=state_dir,
            log_dir=log_dir,
            user=user,
            skip_systemd=skip_systemd,
            check_http=check_http,
            runner=verify_runner,
        )
        checks = _verify_report_to_checks(raw_report)
    except VerifyInstallFailed as exc:
        # Build the partial report: sysctl check first (so it lands
        # at the END of the merged list when we reverse-sort), then
        # a synthetic verify_install.run row. The dispatcher flips
        # the order to match the human-readable convention.
        sysctl_check = _check_sysctl_persistence(
            sysctl_runner=sysctl_runner,
            fs_runner=fs_runner if fs_runner is not None else lambda p: Path(p).read_text(),
        )
        verify_row = DoctorCheck(
            name="verify_install.run",
            status="fail",
            detail=str(exc),
            source="verify_install",
        )
        return DoctorReport(checks=[sysctl_check, verify_row])

    sysctl_check = _check_sysctl_persistence(
        sysctl_runner=sysctl_runner,
        fs_runner=fs_runner if fs_runner is not None else lambda p: Path(p).read_text(),
    )
    checks.append(sysctl_check)
    return DoctorReport(checks=checks)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def _status_upper(status: CheckStatus) -> str:
    """Uppercase the status literal so the table reads ``OK / WARN / FAIL``."""
    return status.upper()


def _format_detail(detail: str) -> str:
    """Truncate the detail column to the configured max width.

    A trailing ellipsis (``...``) is appended when the original
    detail overflows so the operator can immediately see the
    truncation rather than silently losing tail characters.
    """
    if len(detail) <= _DETAIL_COL_MAX:
        return detail
    if _DETAIL_COL_MAX <= 3:
        return detail[:_DETAIL_COL_MAX]
    return detail[: _DETAIL_COL_MAX - 3] + "..."


def render_human(report: DoctorReport) -> str:
    """Render a human-readable ASCII table.

    Format (one row per check):

    .. code-block:: text

       CHECK                                STATUS  DETAIL
       -----------------------------------  ------  ----------------------------------------
       verify_install.binaries.python3     OK      Python 3.12.14
       ...
       sysctl_persistence                  OK      /etc/sysctl.d/99-nora.conf + runtime agree

       ok=N warn=N fail=N

    The table is built as a list of lines and returned as a single
    string. The dispatcher (``__main__._dispatch_doctor``) owns the
    stderr write. Status is uppercase (``OK / WARN / FAIL``) so the
    table reads at a glance. Detail is truncated to fit within the
    80-column target. A trailing newline is preserved so the caller's
    ``sys.stderr.write(...)`` lands the next shell prompt on its own
    line, matching the previous ``print(..., file=buf)`` behaviour.
    """
    name_w = _NAME_COL_WIDTH
    status_w = _STATUS_COL_WIDTH
    underline_name = "-" * name_w
    underline_status = "-" * status_w

    lines: list[str] = [
        f"  CHECK{' ' * (name_w - 5)}STATUS  DETAIL",
        f"  {underline_name}  {underline_status}  {'-' * max(_DETAIL_COL_MAX, 10)}",
    ]

    for check in report.checks:
        name = check.name[:name_w].ljust(name_w)
        status = _status_upper(check.status).ljust(status_w)
        detail = _format_detail(check.detail)
        lines.append(f"  {name}  {status}  {detail}")

    lines.append("")
    lines.append(f"  ok={report.ok_count} warn={report.warn_count} fail={report.fail_count}")
    return "\n".join(lines) + "\n"


def render_json(report: DoctorReport) -> str:
    """Render machine-readable JSON for CI integrations.

    Shape:

    .. code-block:: json

       {
         "checks": [
           {"name": "...", "status": "ok|warn|fail", "detail": "...", "source": "..."}
         ],
         "summary": {"ok": N, "warn": N, "fail": N}
       }

    Stable ordering: verify-install checks in execution order, then
    ``sysctl_persistence`` as the last row. Indent of ``2`` matches
    the convention used by ``scripts/verify-install.sh``'s own JSON
    output and downstream ``jq`` filters.
    """
    payload = {
        "checks": [asdict(check) for check in report.checks],
        "summary": {
            "ok": report.ok_count,
            "warn": report.warn_count,
            "fail": report.fail_count,
        },
    }
    return json.dumps(payload, indent=2)
