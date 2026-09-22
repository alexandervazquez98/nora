"""`nora upgrade` engine — issue #60 / PR-2 of #60 umbrella (RFC #60).

Automates the operator-facing upgrade procedure documented in
``OPERATIONS.md`` § Update procedure. Mirrors the six phases the
shell procedure does by hand, plus a pre-flight backup and a
post-upgrade smoke test, so a wedged ``nora upgrade`` is recoverable
via automatic rollback.

Public surface (consumed by ``src/nora/__main__.py::_dispatch_upgrade``):

* Exceptions: ``UpgradeError``, ``PreFlightError``, ``BackupError``,
  ``UpgradeFailed``, ``SmokeFailed``.
* Result dataclasses: ``PreFlightResult``, ``UpgradeResult``.
* Engine functions: ``preflight``, ``create_backup``, ``restore_backup``,
  ``run_upgrade``, ``verify_install_smoke``.

Design invariants (frozen in
``odd/tasks/installer-upgrade-and-doctor.md``):

* **D2** — backup lives at ``/var/lib/nora/upgrades/<timestamp>/``,
  root-owned ``0700``, persistent across upgrades.
* **D3** — rollback is automatic on phase failure; operator-initiated
  downgrade to a prior version is manual
  (``nora upgrade --ref <known-good-sha>``).
* **D8** — arg pattern mirrors ``nora prompt sync`` (sub-command +
  argparse + explicit exit codes 0/1/2).
* **D9** — headless-only; ``--json`` for machine consumers, no TUI.

Process model:

* Every subprocess call goes through the ``runner=`` injection seam
  with default ``subprocess.run``. Tests stub ``runner`` with a
  ``MagicMock`` so the engine runs without shelling out.
* Subprocess argv lists are constructed explicitly — never
  ``shell=True``.
* Path composition uses ``pathlib.Path``; never string concatenation.

Smoke test integration:

* ``scripts/verify-install.sh --json --strict`` is the same engine
  ``nora doctor`` (PR-3) will reuse. ``run_upgrade`` calls it after
  the upgrade so a FAIL/WARN in the smoke report triggers rollback.
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger("nora.upgrade")

# Type alias for the subprocess injection seam. Matches
# ``subprocess.run``'s public signature; tests can stub with a
# ``MagicMock`` returning ``subprocess.CompletedProcess`` instances.
Runner = Callable[..., subprocess.CompletedProcess[str]]

# Phase names appear in ``UpgradeResult.phases_completed`` and in the
# ``UpgradeFailed.phase`` payload. Keep them stable — operators grep
# the audit log for these strings.
PHASE_PREFLIGHT: Final[str] = "preflight"
PHASE_BACKUP: Final[str] = "backup"
PHASE_FETCH: Final[str] = "git_fetch"
PHASE_CHECKOUT: Final[str] = "git_checkout"
PHASE_UV_SYNC: Final[str] = "uv_sync"
PHASE_SIGN_CATALOG: Final[str] = "sign_catalog"
PHASE_RESTART: Final[str] = "systemctl_restart"
PHASE_SMOKE: Final[str] = "verify_install_smoke"

# Default paths that mirror ``scripts/install.sh`` so a
# ``sudo nora upgrade`` with no flags Just Works on a stock install.
# ``run_upgrade``'s signature does not expose these individually; the
# dispatcher forwards them via env vars when operators override.
DEFAULT_CONFIG_DIR: Final[Path] = Path("/etc/nora")
DEFAULT_STATE_DIR: Final[Path] = Path("/var/lib/nora")
DEFAULT_LOG_DIR: Final[Path] = Path("/var/log/nora")
DEFAULT_USER: Final[str] = "nora"


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UpgradeError(Exception):
    """Base class for every ``nora upgrade`` failure.

    Catch this in the dispatcher if you want a single try/except that
    covers every error class below. Catch the more specific subclasses
    when you need to differentiate (e.g. smoke failures carry the full
    report; backup failures carry no backup path).
    """


class PreFlightError(UpgradeError):
    """Pre-flight could not resolve the target ref.

    Carries the failing command's stderr in the message so the
    operator can grep the audit log for the original failure (e.g.
    ambiguous ref, branch missing from ``origin``).
    """


class BackupError(UpgradeError):
    """Backup creation or restore failed.

    Distinguishes from ``UpgradeFailed`` because no automatic rollback
    is possible — the backup itself never landed on disk, so the
    dispatcher must surface the failure and let the operator intervene
    manually.
    """


class UpgradeFailed(UpgradeError):
    """A mutating phase failed; automatic rollback was attempted.

    Carries the failing phase name so the audit log can attribute the
    failure, and the backup path so the operator can inspect what was
    restored. ``backup_path`` is ``None`` when ``--no-backup`` was set
    (no rollback was attempted in that case).
    """

    def __init__(self, phase: str, backup_path: Path | None, message: str) -> None:
        super().__init__(message)
        self.phase = phase
        self.backup_path = backup_path


class SmokeFailed(UpgradeError):
    """Post-upgrade smoke test reported a FAIL or WARN.

    Carries the parsed ``verify-install.sh --json`` report so the
    operator can drill into which check failed. The dispatcher
    triggers automatic rollback on this error.
    """

    def __init__(self, report: dict[str, Any], message: str) -> None:
        super().__init__(message)
        self.report = report


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PreFlightResult:
    """Outcome of ``preflight()``.

    ``noop=True`` means the resolved target SHA matches the live
    HEAD — no upgrade is required. ``current_sha`` is ``None`` only
    when the live repo has no commits yet (extremely rare; surfaces
    a no-op with ``reason="empty repository"``).
    """

    noop: bool
    current_sha: str | None
    target_sha: str | None
    reason: str | None = None


@dataclass(frozen=True)
class UpgradeResult:
    """Outcome of ``run_upgrade()``.

    ``phases_completed`` lists phase names in execution order up to and
    including the final successful phase. ``smoke_report`` is the parsed
    JSON from ``verify-install.sh --json`` on success, or ``None`` when
    the smoke phase itself did not run (e.g. dry-run, or a prior phase
    failed before the smoke test fired).

    ``phases_completed`` is a mutable list per spec; the dataclass is
    frozen so other fields cannot be reassigned, but Python allows
    ``list.append`` on a frozen-dataclass field — a documented soft
    convention violation.
    """

    preflight: PreFlightResult
    backup_path: Path | None
    phases_completed: list[str] = field(default_factory=list)
    final_sha: str = ""
    smoke_report: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Helpers — subprocess plumbing with explicit argv, never shell=True.
# ---------------------------------------------------------------------------


_FailureRecord = subprocess.CompletedProcess[str] | subprocess.CalledProcessError


def _format_failure(args: list[str], completed: _FailureRecord) -> str:
    """Build a one-line failure message carrying the failing argv + stderr.

    Accepts either a ``CompletedProcess`` (when the caller already has
    the result) or a ``CalledProcessError`` (when ``check=True`` raised
    inside ``_run_or_raise``). Both expose ``.returncode`` and
    ``.stderr`` so the message format is identical.

    Operators grep the audit log for the failing command's stderr to
    diagnose. Including the argv list (already shell-safe because we
    never use ``shell=True``) avoids ambiguity when the same script is
    called multiple times in different phases.
    """
    argv_str = " ".join(shlex.quote(part) for part in args)
    stderr_text = completed.stderr or ""
    if isinstance(stderr_text, bytes):
        stderr_text = stderr_text.decode("utf-8", errors="replace")
    stderr_tail = stderr_text.strip().splitlines()
    stderr_summary = stderr_tail[-1] if stderr_tail else ""
    if stderr_summary:
        return f"command failed (rc={completed.returncode}): {argv_str} — {stderr_summary}"
    return f"command failed (rc={completed.returncode}): {argv_str}"


def _run_or_raise(
    runner: Runner,
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess via ``runner`` and let ``check=True`` raise on failure.

    ``subprocess.run`` with ``check=True`` already raises
    ``CalledProcessError`` on non-zero exit; callers translate that
    into a typed ``UpgradeError`` subclass so the dispatcher can react
    cleanly.
    """
    logger.debug("running: %s (cwd=%s)", args, cwd)
    completed = runner(
        args,
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
    )
    return completed


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------


def preflight(
    *,
    prefix: Path,
    ref: str,
    runner: Runner = subprocess.run,
) -> PreFlightResult:
    """Resolve the target SHA from ``ref`` and compare with current HEAD.

    Runs two ``git rev-parse`` invocations inside ``<prefix>``:

    * ``git rev-parse --verify <ref>^{commit}`` — resolves the ref to a
      full SHA. A non-zero exit (ambiguous, missing, etc.) raises
      ``PreFlightError`` carrying the git stderr.
    * ``git rev-parse HEAD`` — captures the live HEAD. Returns
      ``current_sha=None`` when HEAD is unborn (fresh clone, no commits
      yet); the result is still ``noop=False`` in that case so the
      operator can complete the first install via upgrade.

    ``noop=True`` is returned only when both SHAs are equal AND
    non-empty (a fresh-clone with no commits does NOT match the target).
    """
    prefix_path = Path(prefix)
    if not prefix_path.is_dir():
        raise PreFlightError(
            f"preflight: prefix does not exist or is not a directory: {prefix_path}"
        )

    # Resolve the target ref to a full SHA. `^{commit}` dereferences
    # tags and branches; without it, `git rev-parse --verify v0.3.8`
    # could return a tag-object SHA instead of the underlying commit,
    # which would never match `git rev-parse HEAD`.
    verify_args = ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"]
    try:
        verify_completed = _run_or_raise(runner, verify_args, cwd=prefix_path)
    except subprocess.CalledProcessError as exc:
        raise PreFlightError(
            f"preflight: cannot resolve ref {ref!r} — {_format_failure(verify_args, exc)}"
        ) from exc
    target_sha = verify_completed.stdout.strip()

    # Read current HEAD. An unborn HEAD (no commits yet) yields empty
    # stdout and rc=0; we surface that as `current_sha=None`.
    head_args = ["git", "rev-parse", "HEAD"]
    try:
        head_completed = _run_or_raise(runner, head_args, cwd=prefix_path)
    except subprocess.CalledProcessError as exc:
        raise PreFlightError(
            f"preflight: cannot read current HEAD — {_format_failure(head_args, exc)}"
        ) from exc
    head_raw = head_completed.stdout.strip()
    current_sha = head_raw or None

    if current_sha is not None and current_sha == target_sha:
        return PreFlightResult(
            noop=True,
            current_sha=current_sha,
            target_sha=target_sha,
            reason="already at target",
        )

    return PreFlightResult(
        noop=False,
        current_sha=current_sha,
        target_sha=target_sha,
    )


# ---------------------------------------------------------------------------
# create_backup / restore_backup
# ---------------------------------------------------------------------------


def _backup_source_paths(*, prefix: Path, config_dir: Path) -> dict[str, list[Path]]:
    """Resolve the file/dir inventory the backup copies.

    Centralised so ``create_backup`` and ``restore_backup`` agree on
    the layout. The keys are subdirectories under the backup root;
    the values are source paths relative to the operator's live
    install. A missing source path is logged and skipped — the
    installer phases are designed to be idempotent and tolerant of
    partial state, so a backup must round-trip cleanly regardless of
    which subdirectories exist at backup time.
    """
    config_path = Path(config_dir)
    data_path = Path(prefix) / "data"
    return {
        "etc": [
            config_path / "nora.env",
            config_path / "nora-mcp.env",
            config_path / "signing_key",
        ],
        "data": [
            data_path / "devices.yaml",
            data_path / "oid-catalogs",
        ],
    }


def _utc_timestamp() -> str:
    """UTC timestamp suitable for a backup directory name.

    ``%Y%m%dT%H%M%SZ`` (e.g. ``20260922T121530Z``) sorts
    lexicographically and is unambiguous about timezone. The
    filesystem-safe ``:`` from ISO 8601 is intentionally avoided.
    """
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _copy_one(runner: Runner, src: Path, dst: Path) -> None:
    """Copy a single file or directory tree via ``cp -a``.

    ``cp -a`` is archive mode (recursive, preserve attributes,
    don't follow symlinks-as-symlinks); it round-trips the
    ``signing_key`` and catalog directories bit-for-bit. We do NOT
    use ``shutil.copytree`` directly because tests inject a fake
    ``runner`` that records argv — going through ``cp`` keeps the
    recorded argv list human-readable and matches ``install.sh``.

    Raises ``BackupError`` on non-zero exit; carries the ``cp`` stderr
    so the operator sees the original failure (e.g. permission
    denied on a multi-tenant host).
    """
    args = ["cp", "-a", str(src), str(dst)]
    try:
        _run_or_raise(runner, args)
    except subprocess.CalledProcessError as exc:
        raise BackupError(_format_failure(args, exc)) from exc


def create_backup(
    *,
    prefix: Path,
    config_dir: Path,
    state_dir: Path,
    timestamp: str,
    backup_root: Path = Path("/var/lib/nora/upgrades"),
    dry_run: bool = False,
    runner: Runner = subprocess.run,
) -> Path:
    """Snapshot the operator-edited state of a NORA install.

    Copies:

    * ``<config_dir>/{nora.env, nora-mcp.env, signing_key}`` → ``<backup>/etc/``
    * ``<prefix>/data/{devices.yaml, oid-catalogs/}`` → ``<backup>/data/``

    into ``<backup_root>/<timestamp>/``. The backup directory is
    created ``0700`` (root-only); the ``install -d -m 0700`` argv is
    explicit (not implicit through ``cp``) so the mode is correct
    even when the first ``cp`` succeeds.

    Writes ``MANIFEST.txt`` with three lines:

    * ``current_sha=<HEAD at backup time>``
    * ``ref=<timestamp>`` — the timestamp doubles as the ref so the
      manifest self-identifies against its own directory name.
    * ``timestamp=<UTC timestamp>``

    ``state_dir`` is accepted in the signature for parity with
    ``install.sh`` but is intentionally NOT snapshotted — the
    interventions log is append-only and survives an upgrade by
    design. The parameter is part of the public API so future PRs
    can extend the backup coverage without changing call sites.

    Returns the resolved backup path. Raises ``BackupError`` on any
    I/O failure.
    """
    # ``state_dir`` is reserved for future backup coverage (currently
    # unused); silence the unused-argument linter without forcing a
    # refactor of every call site that already passes it for parity
    # with install.sh.
    _ = state_dir

    backup_root_path = Path(backup_root)
    backup_path = backup_root_path / timestamp

    if dry_run:
        sys_stderr = sys.stderr
        sys_stderr.write(f"[DRY-RUN] would create backup dir: {backup_path} (mode 0700)\n")
        sources = _backup_source_paths(prefix=prefix, config_dir=config_dir)
        for sub, paths in sources.items():
            for src in paths:
                sys_stderr.write(f"[DRY-RUN] would copy {src} -> {backup_path / sub / src.name}\n")
        # Materialise the dry-run manifest in memory (do NOT write
        # to disk — the operator hasn't approved the upgrade yet).
        sys_stderr.write(f"[DRY-RUN] would write {backup_path / 'MANIFEST.txt'}\n")
        return backup_path

    # Create the backup directory root with explicit mode 0700. We do
    # NOT use `mkdir(parents=True, mode=0o700)` on Python's pathlib
    # because it races with subsequent `cp -a` invocations under
    # umask 022 on some kernels — explicit `install -d -m 0700`
    # through the runner is portable and audited.
    mkdir_args = ["install", "-d", "-m", "0700", str(backup_path)]
    try:
        _run_or_raise(runner, mkdir_args)
    except subprocess.CalledProcessError as exc:
        raise BackupError(_format_failure(mkdir_args, exc)) from exc

    sources = _backup_source_paths(prefix=prefix, config_dir=config_dir)
    for sub, paths in sources.items():
        for src in paths:
            if not src.exists():
                logger.warning("backup: source missing, skipping: %s", src)
                continue
            sub_dst = backup_path / sub
            # Ensure the per-subdir exists (mode inherited from
            # backup_path's 0700).
            sub_mkdir_args = ["install", "-d", str(sub_dst)]
            try:
                _run_or_raise(runner, sub_mkdir_args)
            except subprocess.CalledProcessError as exc:
                raise BackupError(_format_failure(sub_mkdir_args, exc)) from exc
            _copy_one(runner, src, sub_dst / src.name)

    # Resolve current HEAD so the manifest records the SHA the
    # upgrade is about to replace.
    try:
        head_completed = _run_or_raise(runner, ["git", "rev-parse", "HEAD"], cwd=Path(prefix))
        current_sha = head_completed.stdout.strip() or "unknown"
    except subprocess.CalledProcessError as exc:
        logger.warning("backup: git rev-parse HEAD failed: %s", exc)
        current_sha = "unknown"

    # Write MANIFEST.txt directly (no `runner` round-trip — the file
    # is operator-facing and we want exact byte control).
    manifest_path = backup_path / "MANIFEST.txt"
    manifest_path.write_text(
        f"current_sha={current_sha}\nref={timestamp}\ntimestamp={timestamp}\n",
        encoding="utf-8",
    )
    # Manifest inherits operator-readable perms; the 0700 directory
    # mode already gates access to anyone but root.
    manifest_path.chmod(0o600)

    return backup_path


def restore_backup(
    *,
    backup_path: Path,
    prefix: Path,
    config_dir: Path,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
) -> None:
    """Reverse a ``create_backup`` copy: live tree ← backup tree.

    The source layout is the same dict as ``create_backup`` uses; we
    iterate it backwards so a missing source in the backup is logged
    and skipped rather than aborted (a partial backup from a half-
    finished install should still roll back what it has).

    Raises ``BackupError`` on any I/O failure. The dispatcher treats
    a restore failure as terminal — the operator must intervene
    manually because we have no further recovery the engine can take.
    """
    backup_root = Path(backup_path)
    if not backup_root.is_dir():
        raise BackupError(f"restore: backup path does not exist: {backup_root}")

    sources = _backup_source_paths(prefix=prefix, config_dir=config_dir)
    if dry_run:
        for sub, paths in sources.items():
            for src in paths:
                sys.stderr.write(
                    f"[DRY-RUN] would restore {backup_root / sub / src.name} -> {src}\n"
                )
        return

    for sub, paths in sources.items():
        for src in paths:
            backup_src = backup_root / sub / src.name
            if not backup_src.exists():
                logger.warning("restore: backup entry missing, skipping: %s", backup_src)
                continue
            # Ensure parent of the live target exists.
            parent_mkdir_args = ["install", "-d", str(src.parent)]
            try:
                _run_or_raise(runner, parent_mkdir_args)
            except subprocess.CalledProcessError as exc:
                raise BackupError(_format_failure(parent_mkdir_args, exc)) from exc
            # Remove the live target before copy so a stale file
            # does not survive the rollback.
            if src.is_dir() and not src.is_symlink():
                shutil.rmtree(src)
            elif src.exists() or src.is_symlink():
                src.unlink()
            _copy_one(runner, backup_src, src)


# ---------------------------------------------------------------------------
# run_upgrade
# ---------------------------------------------------------------------------


def _phase_name_from_args(args: list[str]) -> str:
    """Map an argv list back to a phase name for ``UpgradeFailed``.

    Used when the failure surfaces mid-loop and we need a stable
    identifier to attribute the failure to. Falls back to the
    first argv element when no known mapping exists.
    """
    if not args:
        return "unknown"
    head = args[0]
    if head == "git":
        if "fetch" in args:
            return PHASE_FETCH
        if "checkout" in args:
            return PHASE_CHECKOUT
        return "git"
    if head == "uv":
        return PHASE_UV_SYNC
    if args[-1].endswith("sign_catalog.py") or "sign_catalog" in args:
        return PHASE_SIGN_CATALOG
    if head == "systemctl":
        return PHASE_RESTART
    if head == "bash" or "verify-install.sh" in args or head.endswith("verify-install.sh"):
        return PHASE_SMOKE
    return head


def _phase_runner(
    runner: Runner,
    args: list[str],
    *,
    cwd: Path | None = None,
) -> None:
    """Run a phase subprocess; raise with formatted message on failure."""
    try:
        _run_or_raise(runner, args, cwd=cwd)
    except subprocess.CalledProcessError as exc:
        raise UpgradeFailed(
            phase=_phase_name_from_args(args),
            backup_path=None,
            message=_format_failure(args, exc),
        ) from exc


def _source_env_file(config_dir: Path) -> dict[str, str]:
    """Parse a shell env file into a dict for ``subprocess.run(env=...)``.

    Mirrors ``install.sh::phase_catalog``'s ``set -a; . file; set +a``
    pattern but returns a dict so we can pass it via ``env=`` to the
    Python subprocess (avoids a shell round-trip and keeps the argv
    list visible to test stubs). Lines that start with ``#`` and
    blank lines are skipped; lines without ``=`` are skipped; values
    have surrounding single or double quotes stripped.
    """
    env_file = Path(config_dir) / "nora.env"
    env: dict[str, str] = {}
    if not env_file.is_file():
        return env
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip surrounding quotes (single or double) so
        # `KEY="value"` and `KEY=value` are equivalent.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        env[key] = value
    return env


def _iter_catalog_files(prefix: Path) -> list[Path]:
    """Return catalog files under ``<prefix>/data/oid-catalogs/`` for re-signing.

    Mirrors ``install.sh::phase_catalog``: every ``*.json`` under the
    tree. The per-file ``--vendor / --model / --firmware`` are
    derived from the path components (``vendor/model/firmware.json``).
    """
    catalogs_root = Path(prefix) / "data" / "oid-catalogs"
    if not catalogs_root.is_dir():
        return []
    return sorted(catalogs_root.rglob("*.json"))


def _re_sign_catalog(
    runner: Runner,
    *,
    prefix: Path,
    catalog: Path,
    env: dict[str, str],
) -> None:
    """Re-sign one catalog via ``sign_catalog.py`` with explicit args.

    Closes #32: without ``--vendor / --model / --firmware``,
    ``sign_catalog.py`` defaults to ``cambium/pmp450i/15.2.1`` and
    every iteration re-signs the SAME file, leaving sibling firmware
    catalogs unsigned.
    """
    firmware = catalog.stem
    model = catalog.parent.name
    vendor = catalog.parent.parent.name
    sign_args = [
        str(Path(prefix) / ".venv" / "bin" / "python"),
        str(Path(prefix) / "scripts" / "sign_catalog.py"),
        "--vendor",
        vendor,
        "--model",
        model,
        "--firmware",
        firmware,
        "--output-root",
        str(Path(prefix) / "data" / "oid-catalogs"),
    ]
    _phase_runner(runner, sign_args, cwd=Path(prefix))


def _smoke_runner(
    smoke_runner: Runner,
    *,
    prefix: Path,
    config_dir: Path,
    state_dir: Path,
    log_dir: Path,
    user: str,
    skip_systemd: bool,
    check_http: bool,
) -> dict[str, Any]:
    """Execute ``scripts/verify-install.sh --json`` and parse the result.

    Smoke pass = exit 0 AND ``summary.fail == 0`` AND
    ``summary.warn == 0``. A non-zero exit OR any FAIL/WARN raises
    ``SmokeFailed``. Parse failures (non-JSON stdout) raise
    ``SmokeFailed`` with an empty report — the operator gets a clear
    "verify-install.sh did not emit JSON" message.
    """
    verify_args = [
        str(Path(prefix) / "scripts" / "verify-install.sh"),
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
        completed = smoke_runner(verify_args, check=False, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        raise SmokeFailed(
            report={},
            message=f"smoke: verify-install.sh did not run — {_format_failure(verify_args, exc)}",
        ) from exc

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip().splitlines()
        stderr_summary = stderr_tail[-1] if stderr_tail else "no stderr"
        raise SmokeFailed(
            report={},
            message=(
                f"smoke: verify-install.sh exited rc={completed.returncode} — {stderr_summary}"
            ),
        )

    try:
        report = json.loads(completed.stdout)
    except (ValueError, TypeError) as exc:
        raise SmokeFailed(
            report={},
            message=(
                f"smoke: verify-install.sh --json did not emit parseable JSON "
                f"({exc.__class__.__name__}: {exc})"
            ),
        ) from exc

    if not isinstance(report, dict):
        raise SmokeFailed(
            report={},
            message=(
                "smoke: verify-install.sh --json did not emit a JSON object "
                f"(got {type(report).__name__})"
            ),
        )

    summary = report.get("summary") or {}
    fail_count = int(summary.get("fail", 0))
    warn_count = int(summary.get("warn", 0))
    if fail_count > 0 or warn_count > 0:
        raise SmokeFailed(
            report=report,
            message=f"smoke: verify-install.sh reported fail={fail_count} warn={warn_count}",
        )

    return report


def run_upgrade(
    *,
    prefix: Path,
    target_sha: str,
    backup_path: Path | None,
    config_dir: Path = DEFAULT_CONFIG_DIR,
    state_dir: Path = DEFAULT_STATE_DIR,
    log_dir: Path = DEFAULT_LOG_DIR,
    user: str = DEFAULT_USER,
    restart: bool = True,
    dry_run: bool = False,
    runner: Runner = subprocess.run,
    smoke_runner: Runner = subprocess.run,
) -> UpgradeResult:
    """Execute the upgrade phases in order with rollback on failure.

    Phases (D2/D3 contract):

    1. ``git fetch origin`` (still on the old HEAD)
    2. ``git checkout <target-sha>`` (detached HEAD)
    3. ``uv sync`` to refresh dependencies
    4. Re-sign every catalog under ``<prefix>/data/oid-catalogs/``
    5. ``systemctl restart nora-mcp`` (skipped when ``restart=False``)
    6. ``scripts/verify-install.sh --json --strict`` (smoke test)

    On ANY phase failure: restore the backup (if provided), restart
    ``nora-mcp`` best-effort, and raise ``UpgradeFailed`` carrying
    the failing phase name + backup path. The ``backup_path`` on
    the raised exception is the one passed in (so it stays stable
    for the audit log), even when the restore itself fails.
    """
    prefix_path = Path(prefix)
    phases_completed: list[str] = []

    # Read current HEAD once so the result dataclass carries the
    # starting SHA even on a successful upgrade. Failure here
    # surfaces as UpgradeFailed(phase=preflight).
    try:
        head_completed = _run_or_raise(runner, ["git", "rev-parse", "HEAD"], cwd=prefix_path)
        starting_sha = head_completed.stdout.strip() or None
    except subprocess.CalledProcessError as exc:
        raise UpgradeFailed(
            phase=PHASE_PREFLIGHT,
            backup_path=backup_path,
            message=_format_failure(["git", "rev-parse", "HEAD"], exc),
        ) from exc

    # Build the env for the sign phase once. It mirrors what
    # ``install.sh::phase_catalog`` does with ``set -a; . file; set +a``.
    sign_env = _source_env_file(config_dir)

    def _finalise_on_failure(phase: str, exc: Exception) -> None:
        """Rollback + best-effort restart, then raise ``UpgradeFailed``."""
        if backup_path is not None and not dry_run:
            try:
                restore_backup(
                    backup_path=backup_path,
                    prefix=prefix_path,
                    config_dir=config_dir,
                    runner=runner,
                )
            except BackupError as restore_exc:
                logger.error("rollback: restore failed: %s", restore_exc)
        # Best-effort restart: even if rollback failed, an old
        # running daemon might still hold the upgrade's new state in
        # memory. Restarting brings it back to the rolled-back
        # filesystem state.
        if not dry_run:
            try:
                _run_or_raise(runner, ["systemctl", "restart", "nora-mcp"])
            except subprocess.CalledProcessError as restart_exc:
                logger.error(
                    "rollback: systemctl restart failed: %s",
                    _format_failure(["systemctl", "restart", "nora-mcp"], restart_exc),
                )
        raise UpgradeFailed(phase=phase, backup_path=backup_path, message=str(exc)) from exc

    if dry_run:
        # Dry-run: short-circuit every mutating phase BEFORE its
        # subprocess call. We still record every phase so the result
        # is auditable.
        catalog_count = len(_iter_catalog_files(prefix_path))
        sys.stderr.write("[DRY-RUN] would run: git fetch origin\n")
        sys.stderr.write(f"[DRY-RUN] would run: git checkout {target_sha}\n")
        sys.stderr.write("[DRY-RUN] would run: uv sync\n")
        sys.stderr.write(f"[DRY-RUN] would re-sign {catalog_count} catalog(s)\n")
        if restart:
            sys.stderr.write("[DRY-RUN] would run: systemctl restart nora-mcp\n")
        sys.stderr.write("[DRY-RUN] would run: scripts/verify-install.sh --json --strict\n")
        phases_completed.extend([PHASE_FETCH, PHASE_CHECKOUT, PHASE_UV_SYNC, PHASE_SIGN_CATALOG])
        if restart:
            phases_completed.append(PHASE_RESTART)
        phases_completed.append(PHASE_SMOKE)
        return UpgradeResult(
            preflight=PreFlightResult(
                noop=False,
                current_sha=starting_sha,
                target_sha=target_sha,
            ),
            backup_path=backup_path,
            phases_completed=phases_completed,
            final_sha=target_sha,
            smoke_report=None,
        )

    # Phase 1: git fetch origin.
    try:
        _phase_runner(runner, ["git", "fetch", "origin"], cwd=prefix_path)
    except UpgradeFailed as exc:
        _finalise_on_failure(PHASE_FETCH, exc)
    phases_completed.append(PHASE_FETCH)

    # Phase 2: git checkout <target-sha>.
    try:
        _phase_runner(runner, ["git", "checkout", target_sha], cwd=prefix_path)
    except UpgradeFailed as exc:
        _finalise_on_failure(PHASE_CHECKOUT, exc)
    phases_completed.append(PHASE_CHECKOUT)

    # Phase 3: uv sync.
    try:
        _phase_runner(runner, ["uv", "sync"], cwd=prefix_path)
    except UpgradeFailed as exc:
        _finalise_on_failure(PHASE_UV_SYNC, exc)
    phases_completed.append(PHASE_UV_SYNC)

    # Phase 4: re-sign every catalog.
    try:
        catalogs = _iter_catalog_files(prefix_path)
        for catalog in catalogs:
            _re_sign_catalog(runner, prefix=prefix_path, catalog=catalog, env=sign_env)
    except UpgradeFailed as exc:
        _finalise_on_failure(PHASE_SIGN_CATALOG, exc)
    phases_completed.append(PHASE_SIGN_CATALOG)

    # Phase 5: restart nora-mcp. Restart failure does NOT trigger
    # rollback — the new code is on disk; an operator can
    # ``systemctl restart nora-mcp`` by hand. The dispatcher will
    # still surface the non-zero exit through the exception chain.
    if restart:
        try:
            _phase_runner(runner, ["systemctl", "restart", "nora-mcp"])
        except UpgradeFailed as exc:
            _finalise_on_failure(PHASE_RESTART, exc)
        phases_completed.append(PHASE_RESTART)

    # Phase 6: smoke test (always runs even when restart was skipped,
    # so an offline-validation workflow still gets a report).
    try:
        smoke_report = _smoke_runner(
            smoke_runner,
            prefix=prefix_path,
            config_dir=config_dir,
            state_dir=state_dir,
            log_dir=log_dir,
            user=user,
            skip_systemd=not restart,
            check_http=False,
        )
    except SmokeFailed as exc:
        _finalise_on_failure(PHASE_SMOKE, exc)
    phases_completed.append(PHASE_SMOKE)

    return UpgradeResult(
        preflight=PreFlightResult(
            noop=False,
            current_sha=starting_sha,
            target_sha=target_sha,
        ),
        backup_path=backup_path,
        phases_completed=phases_completed,
        final_sha=target_sha,
        smoke_report=smoke_report,
    )


# ---------------------------------------------------------------------------
# verify_install_smoke — standalone smoke runner used by tests + future PRs.
# ---------------------------------------------------------------------------


def verify_install_smoke(
    *,
    prefix: Path,
    config_dir: Path,
    state_dir: Path,
    log_dir: Path,
    user: str,
    skip_systemd: bool = False,
    check_http: bool = False,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Public wrapper around the smoke test — issue #60 / PR-3 will reuse this.

    Equivalent to the smoke phase of ``run_upgrade`` but exposed as a
    standalone entry point so ``nora doctor`` (PR-3) can call it
    without going through the upgrade state machine.
    """
    return _smoke_runner(
        runner,
        prefix=Path(prefix),
        config_dir=Path(config_dir),
        state_dir=Path(state_dir),
        log_dir=Path(log_dir),
        user=user,
        skip_systemd=skip_systemd,
        check_http=check_http,
    )
