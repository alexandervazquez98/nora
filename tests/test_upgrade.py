"""Tests for ``nora.upgrade`` + the ``nora upgrade`` dispatcher.

Issue #60 / PR-2 — automate the OPERATIONS.md update procedure with
pre-flight backup, phase execution, and automatic rollback. Tests
exercise the engine via the ``runner=`` injection seam so no real
``git``, ``uv``, ``cp``, or ``systemctl`` invocation ever fires.

Patterns:

* ``runner`` is stubbed per-test with either a ``MagicMock`` (pure
  call-order assertion) or a small ``_FileBackedRunner`` (record
  argv + actually perform the ``cp -a`` / ``install -d`` calls so
  round-trip tests can mutate + restore the live tree).
* The ``nora upgrade`` dispatcher tests mirror the
  ``test_nora_prompt_sync_*`` pattern: monkeypatch the upgrade
  module functions + skip the MCP boot path
  (``monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)``).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nora.upgrade import (
    PHASE_CHECKOUT,
    PHASE_FETCH,
    PHASE_RESTART,
    PHASE_SIGN_CATALOG,
    PHASE_SMOKE,
    PHASE_UV_SYNC,
    BackupError,
    PreFlightError,
    PreFlightResult,
    SmokeFailed,
    UpgradeFailed,
    UpgradeResult,
    _source_env_file,
    create_backup,
    preflight,
    restore_backup,
    run_upgrade,
    verify_install_smoke,
)

# ---------------------------------------------------------------------------
# Helpers — fake subprocess runners used by tests.
# ---------------------------------------------------------------------------


def _ok_completed(
    args: list[str], stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    """Build a ``CompletedProcess`` mirroring ``subprocess.run(check=True)``."""
    return subprocess.CompletedProcess(args=args, returncode=0, stdout=stdout, stderr=stderr)


class _FileBackedRunner:
    """Test double that records argv AND performs ``cp -a`` / ``install -d``.

    Lets round-trip tests mutate a real ``tmp_path`` tree through the
    same argv list the production code builds, while keeping git /
    uv / systemctl fully stubbed.

    The ``git_sha_map`` argument maps a ``git rev-parse`` argv to its
    stdout payload so tests can pin current + target SHAs. A ``None``
    value makes the runner raise ``CalledProcessError`` (used by
    ``test_preflight_fails_when_ref_unresolvable``).
    """

    def __init__(
        self,
        tmp_path: Path,
        git_sha_map: dict[tuple[str, ...], str | None] | None = None,
        git_extra_map: dict[tuple[str, ...], str | None] | None = None,
        fail_argv: tuple[str, ...] | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.calls: list[list[str]] = []
        self.git_sha_map = git_sha_map or {}
        self.git_extra_map = git_extra_map or {}
        self.fail_argv = fail_argv

    def __call__(
        self,
        args: list[str],
        /,
        *,
        check: bool = True,
        capture_output: bool = True,
        text: bool = False,  # noqa: ARG002 — matches subprocess.run signature
        cwd: str | None = None,  # noqa: ARG002 — recorded via argv only
        env: dict[str, str] | None = None,  # noqa: ARG002 — recorded via argv only
    ) -> subprocess.CompletedProcess[str]:
        argv = tuple(args)
        self.calls.append(list(args))

        # Honour explicit failure injection.
        if self.fail_argv is not None and argv == self.fail_argv:
            completed = subprocess.CompletedProcess(
                args=args, returncode=1, stdout="", stderr="forced failure"
            )
            if check:
                raise subprocess.CalledProcessError(
                    returncode=1, cmd=args, output="", stderr="forced failure"
                )
            return completed

        head = args[0] if args else ""
        if head == "git":
            return self._run_git(args, check)
        if head == "cp":
            return self._run_cp(args)
        if head == "install":
            return self._run_install(args)
        # uv / systemctl / sign_catalog / verify-install / any other
        # command: succeed silently.
        return _ok_completed(args)

    # ----- dispatchers -------------------------------------------------

    def _run_git(self, args: list[str], check: bool) -> subprocess.CompletedProcess[str]:
        argv = tuple(args)
        if argv in self.git_sha_map:
            payload = self.git_sha_map[argv]
            if payload is None:
                completed = subprocess.CompletedProcess(
                    args=args, returncode=128, stdout="", stderr="fatal: bad ref"
                )
                if check:
                    raise subprocess.CalledProcessError(
                        returncode=128, cmd=args, output="", stderr="fatal: bad ref"
                    )
                return completed
            return _ok_completed(args, stdout=payload)
        if argv in self.git_extra_map:
            payload = self.git_extra_map[argv]
            if payload is None:
                completed = subprocess.CompletedProcess(
                    args=args, returncode=128, stdout="", stderr="fatal: extra"
                )
                if check:
                    raise subprocess.CalledProcessError(
                        returncode=128, cmd=args, output="", stderr="fatal: extra"
                    )
                return completed
            return _ok_completed(args, stdout=payload)
        # Default: every other git call succeeds with empty stdout.
        return _ok_completed(args)

    def _run_cp(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        # cp -a <src> <dst>
        if len(args) >= 4 and args[1] == "-a":
            src = Path(args[2])
            dst = Path(args[3])
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        return _ok_completed(args)

    def _run_install(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        # install -d [-m MODE] <path>...
        if len(args) >= 2 and args[1] == "-d":
            mode: int | None = None
            i = 2
            paths: list[Path] = []
            while i < len(args):
                if args[i] == "-m" and i + 1 < len(args):
                    mode = int(args[i + 1], 8)
                    i += 2
                else:
                    paths.append(Path(args[i]))
                    i += 1
            for path in paths:
                path.mkdir(parents=True, exist_ok=True)
                if mode is not None:
                    path.chmod(mode)
        return _ok_completed(args)


def _git_argv(*parts: str) -> tuple[str, ...]:
    """Helper for the tests — builds a tuple key from a git argv prefix."""
    return ("git", *parts)


# ---------------------------------------------------------------------------
# Tests 1-3: preflight
# ---------------------------------------------------------------------------


def test_preflight_returns_target_sha(tmp_path: Path) -> None:
    """preflight resolves the ref AND reads current HEAD.

    Fake runner returns ``abc123`` for ``git rev-parse --verify
    origin/main^{commit}`` and ``def456`` for ``git rev-parse HEAD``.
    Asserts the result carries both SHAs and ``noop=False``.
    """
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    runner = _FileBackedRunner(
        tmp_path,
        git_sha_map={
            _git_argv("rev-parse", "--verify", "origin/main^{commit}"): "abc123",
            _git_argv("rev-parse", "HEAD"): "def456",
        },
    )

    result = preflight(prefix=prefix, ref="origin/main", runner=runner)

    assert isinstance(result, PreFlightResult)
    assert result.noop is False
    assert result.current_sha == "def456"
    assert result.target_sha == "abc123"
    assert result.reason is None


def test_preflight_noop_when_current_equals_target(tmp_path: Path) -> None:
    """preflight returns ``noop=True, reason='already at target'`` when SHAs match."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    runner = _FileBackedRunner(
        tmp_path,
        git_sha_map={
            _git_argv("rev-parse", "--verify", "origin/main^{commit}"): "abc123",
            _git_argv("rev-parse", "HEAD"): "abc123",
        },
    )

    result = preflight(prefix=prefix, ref="origin/main", runner=runner)

    assert result.noop is True
    assert result.current_sha == "abc123"
    assert result.target_sha == "abc123"
    assert result.reason == "already at target"


def test_preflight_fails_when_ref_unresolvable(tmp_path: Path) -> None:
    """preflight raises ``PreFlightError`` when the ref cannot be resolved."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    runner = _FileBackedRunner(
        tmp_path,
        git_sha_map={
            _git_argv("rev-parse", "--verify", "does-not-exist^{commit}"): None,
        },
    )

    with pytest.raises(PreFlightError):
        preflight(prefix=prefix, ref="does-not-exist", runner=runner)


# ---------------------------------------------------------------------------
# Tests 4-5: create_backup
# ---------------------------------------------------------------------------


def test_create_backup_copies_files_and_writes_manifest(tmp_path: Path) -> None:
    """Backup copies the source tree + writes MANIFEST.txt with current_sha + timestamp.

    Sets up a real source tree under ``<tmp>/etc`` + ``<tmp>/data``
    so the file-backed runner actually performs the cp / install calls.
    Asserts every captured argv + the MANIFEST contents.
    """
    prefix = tmp_path / "prefix"
    config_dir = tmp_path / "etc"
    state_dir = tmp_path / "var-lib"
    config_dir.mkdir(parents=True)
    (config_dir / "nora.env").write_text("NORA_OID_CATALOG_SIGNING_KEY=abc\n")
    (config_dir / "nora-mcp.env").write_text("NORA_MCP_TRANSPORT=stdio\n")
    (config_dir / "signing_key").write_text("secret-key\n")
    (prefix / "data").mkdir(parents=True)
    (prefix / "data" / "devices.yaml").write_text("devices: []\n")
    catalog_dir = prefix / "data" / "oid-catalogs" / "cambium" / "pmp450i"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "15.2.1.json").write_text('{"v": 1}\n')

    backup_root = tmp_path / "backups"
    runner = _FileBackedRunner(
        tmp_path,
        git_sha_map={_git_argv("rev-parse", "HEAD"): "deadbeef"},
    )

    backup_path = create_backup(
        prefix=prefix,
        config_dir=config_dir,
        state_dir=state_dir,
        timestamp="20260922T121530Z",
        backup_root=backup_root,
        runner=runner,
    )

    assert backup_path == backup_root / "20260922T121530Z"
    assert backup_path.is_dir()
    # 0700 mode (best-effort — chmod on tmpfs is reliable on Linux/macOS).
    assert (backup_path.stat().st_mode & 0o777) == 0o700
    # MANIFEST.txt was written with the expected keys.
    manifest = (backup_path / "MANIFEST.txt").read_text(encoding="utf-8")
    assert "current_sha=deadbeef" in manifest
    assert "timestamp=20260922T121530Z" in manifest
    # Source files round-tripped.
    assert (backup_path / "etc" / "nora.env").is_file()
    assert (backup_path / "etc" / "signing_key").read_text(encoding="utf-8") == "secret-key\n"
    assert (backup_path / "data" / "devices.yaml").is_file()
    assert (backup_path / "data" / "oid-catalogs" / "cambium" / "pmp450i" / "15.2.1.json").is_file()
    # Runner saw the expected sequence of mutating commands.
    argv_heads = [c[0] for c in runner.calls]
    assert argv_heads[0] == "install"
    assert "install" in argv_heads
    assert "cp" in argv_heads
    assert "git" in argv_heads


def test_create_backup_raises_on_copy_failure(tmp_path: Path) -> None:
    """``cp`` failure surfaces as ``BackupError`` carrying the argv + stderr."""
    prefix = tmp_path / "prefix"
    config_dir = tmp_path / "etc"
    state_dir = tmp_path / "var-lib"
    config_dir.mkdir(parents=True)
    (config_dir / "nora.env").write_text("NORA_OID_CATALOG_SIGNING_KEY=abc\n")
    backup_root = tmp_path / "backups"
    expected_cp_argv = (
        "cp",
        "-a",
        str(config_dir / "nora.env"),
        str(backup_root / "20260922T121530Z" / "etc" / "nora.env"),
    )
    runner = _FileBackedRunner(
        tmp_path,
        git_sha_map={_git_argv("rev-parse", "HEAD"): "deadbeef"},
        fail_argv=expected_cp_argv,
    )

    with pytest.raises(BackupError):
        create_backup(
            prefix=prefix,
            config_dir=config_dir,
            state_dir=state_dir,
            timestamp="20260922T121530Z",
            backup_root=backup_root,
            runner=runner,
        )


# ---------------------------------------------------------------------------
# Test 6: restore_backup round-trip
# ---------------------------------------------------------------------------


def test_restore_backup_round_trip(tmp_path: Path) -> None:
    """Backup + mutate + restore returns the live tree to the backup state."""
    prefix = tmp_path / "prefix"
    config_dir = tmp_path / "etc"
    state_dir = tmp_path / "var-lib"
    config_dir.mkdir(parents=True)
    (config_dir / "nora.env").write_text("NORA_OID_CATALOG_SIGNING_KEY=v1\n")
    (config_dir / "signing_key").write_text("key-v1\n")
    (prefix / "data").mkdir(parents=True)
    (prefix / "data" / "devices.yaml").write_text("devices: []\n")
    backup_root = tmp_path / "backups"
    runner = _FileBackedRunner(
        tmp_path,
        git_sha_map={_git_argv("rev-parse", "HEAD"): "deadbeef"},
    )

    # Snapshot v1.
    backup_path = create_backup(
        prefix=prefix,
        config_dir=config_dir,
        state_dir=state_dir,
        timestamp="20260922T121530Z",
        backup_root=backup_root,
        runner=runner,
    )

    # Mutate the live tree to v2.
    (config_dir / "nora.env").write_text("NORA_OID_CATALOG_SIGNING_KEY=v2\n")
    (config_dir / "signing_key").write_text("key-v2\n")
    (prefix / "data" / "devices.yaml").write_text("devices: [mutated]\n")

    # Restore.
    restore_runner = _FileBackedRunner(tmp_path)
    restore_backup(
        backup_path=backup_path,
        prefix=prefix,
        config_dir=config_dir,
        runner=restore_runner,
    )

    # Live tree matches the v1 backup.
    assert (config_dir / "nora.env").read_text(encoding="utf-8") == (
        "NORA_OID_CATALOG_SIGNING_KEY=v1\n"
    )
    assert (config_dir / "signing_key").read_text(encoding="utf-8") == "key-v1\n"
    assert (prefix / "data" / "devices.yaml").read_text(encoding="utf-8") == "devices: []\n"


# ---------------------------------------------------------------------------
# Tests 7-10: run_upgrade phase order + rollback + restart + dry-run
# ---------------------------------------------------------------------------


def _build_installed_prefix(tmp_path: Path) -> Path:
    """Build a fake ``<prefix>`` with one catalog under ``data/oid-catalogs``.

    The catalog is required so the ``run_upgrade`` sign phase has
    something to iterate over (without it the phase becomes a no-op
    which still passes but doesn't exercise the per-catalog loop).
    """
    prefix = tmp_path / "prefix"
    (prefix / "data" / "oid-catalogs" / "cambium" / "pmp450i").mkdir(parents=True)
    (prefix / "data" / "oid-catalogs" / "cambium" / "pmp450i" / "15.2.1.json").write_text(
        '{"v": 1}\n'
    )
    return prefix


def test_run_upgrade_calls_phases_in_order(tmp_path: Path) -> None:
    """The phase sequence is git fetch → git checkout → uv sync → sign → restart → smoke."""
    prefix = _build_installed_prefix(tmp_path)
    runner = MagicMock(
        side_effect=[
            _ok_completed(["git", "rev-parse", "HEAD"], stdout="old_sha\n"),
            _ok_completed(["git", "fetch", "origin"]),
            _ok_completed(["git", "checkout", "new_sha"]),
            _ok_completed(["uv", "sync"]),
            # sign_catalog per catalog.
            _ok_completed(["<sign-catalog>"]),
            _ok_completed(["systemctl", "restart", "nora-mcp"]),
            _ok_completed(
                ["verify-install.sh"],
                stdout=json.dumps(
                    {
                        "checks": [],
                        "summary": {"ok": 5, "warn": 0, "fail": 0},
                    }
                ),
            ),
        ]
    )

    backup_path = tmp_path / "backup"
    backup_path.mkdir()
    result = run_upgrade(
        prefix=prefix,
        target_sha="new_sha",
        backup_path=backup_path,
        runner=runner,
        smoke_runner=runner,
    )

    # Argv list — one per recorded call.
    argvs = [call.args[0] for call in runner.call_args_list]
    assert argvs[0][:2] == ["git", "rev-parse"], (
        f"first call must capture starting SHA; got {argvs[0]!r}"
    )
    assert any(c[:2] == ["git", "fetch"] for c in argvs), f"git fetch must run; argvs={argvs!r}"
    assert any(c[:2] == ["git", "checkout"] for c in argvs), (
        f"git checkout must run; argvs={argvs!r}"
    )
    assert any(c[:2] == ["uv", "sync"] for c in argvs), f"uv sync must run; argvs={argvs!r}"
    assert any(any(p.endswith("sign_catalog.py") for p in c) for c in argvs), (
        f"sign_catalog must run per-catalog; argvs={argvs!r}"
    )
    assert any(c[:2] == ["systemctl", "restart"] for c in argvs), (
        f"systemctl restart must run; argvs={argvs!r}"
    )
    assert any(any("verify-install.sh" in p for p in c) for c in argvs), (
        f"smoke test must run; argvs={argvs!r}"
    )

    assert isinstance(result, UpgradeResult)
    assert result.phases_completed == [
        PHASE_FETCH,
        PHASE_CHECKOUT,
        PHASE_UV_SYNC,
        PHASE_SIGN_CATALOG,
        PHASE_RESTART,
        PHASE_SMOKE,
    ]
    assert result.final_sha == "new_sha"
    assert result.backup_path == backup_path
    assert isinstance(result.smoke_report, dict)
    assert result.smoke_report["summary"]["fail"] == 0


def test_run_upgrade_rolls_back_on_phase_failure(tmp_path: Path) -> None:
    """A failed phase triggers restore_backup + best-effort systemctl restart."""
    prefix = _build_installed_prefix(tmp_path)

    # The dispatcher feeds run_upgrade the backup_path; the upgrade
    # module then calls restore_backup inside the failure path. The
    # restore_backup walks the same source layout the create_backup
    # uses, so we materialise stub entries so the cp -a calls actually
    # fire (and get recorded).
    backup_root = tmp_path / "backups"
    backup_path = backup_root / "20260922T121530Z"
    backup_path.mkdir(parents=True)
    (backup_path / "etc").mkdir()
    (backup_path / "etc" / "nora.env").write_text("x=1\n")
    (backup_path / "etc" / "nora-mcp.env").write_text("x=1\n")
    (backup_path / "etc" / "signing_key").write_text("k\n")
    (backup_path / "data").mkdir()
    (backup_path / "data" / "devices.yaml").write_text("d: []\n")
    (backup_path / "data" / "oid-catalogs").mkdir()

    def side_effect(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # Reject the sign_catalog call so the rollback path fires.
        if any(p.endswith("sign_catalog.py") for p in args):
            raise subprocess.CalledProcessError(
                returncode=1, cmd=args, output="", stderr="hmac mismatch"
            )
        return _ok_completed(args)

    runner = MagicMock(side_effect=side_effect)

    with pytest.raises(UpgradeFailed) as excinfo:
        run_upgrade(
            prefix=prefix,
            target_sha="new_sha",
            backup_path=backup_path,
            runner=runner,
            smoke_runner=runner,
        )

    err = excinfo.value
    assert err.phase == PHASE_SIGN_CATALOG
    assert err.backup_path == backup_path
    # Rollback invoked restore_backup (install -d + cp -a observed)
    # AND the post-rollback best-effort systemctl restart.
    argvs = [call.args[0] for call in runner.call_args_list]
    assert any(c == ["systemctl", "restart", "nora-mcp"] for c in argvs), (
        f"systemctl restart must fire best-effort after rollback; argvs={argvs!r}"
    )
    assert any(c[0] == "cp" for c in argvs), f"restore_backup must call cp -a; argvs={argvs!r}"


def test_run_upgrade_no_restart_skips_systemctl(tmp_path: Path) -> None:
    """``restart=False`` skips the systemctl restart phase entirely."""
    prefix = _build_installed_prefix(tmp_path)
    runner = MagicMock(
        side_effect=[
            _ok_completed(["git", "rev-parse", "HEAD"], stdout="old_sha\n"),
            _ok_completed(["git", "fetch", "origin"]),
            _ok_completed(["git", "checkout", "new_sha"]),
            _ok_completed(["uv", "sync"]),
            _ok_completed(["<sign-catalog>"]),
            _ok_completed(
                ["verify-install.sh"],
                stdout=json.dumps({"checks": [], "summary": {"ok": 5, "warn": 0, "fail": 0}}),
            ),
        ]
    )

    result = run_upgrade(
        prefix=prefix,
        target_sha="new_sha",
        backup_path=None,
        restart=False,
        runner=runner,
        smoke_runner=runner,
    )

    argvs = [call.args[0] for call in runner.call_args_list]
    assert not any(c[:2] == ["systemctl", "restart"] for c in argvs), (
        f"systemctl restart must NOT run when restart=False; argvs={argvs!r}"
    )
    # Smoke test still ran (offline-validation path).
    assert any(any("verify-install.sh" in p for p in c) for c in argvs)
    assert PHASE_RESTART not in result.phases_completed
    assert PHASE_SMOKE in result.phases_completed


def test_run_upgrade_dry_run_does_not_call_subprocess_for_mutating_phases(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``dry_run=True`` records no mutating subprocess calls — only pre-flight reads."""
    prefix = _build_installed_prefix(tmp_path)
    runner = MagicMock(
        side_effect=[
            _ok_completed(["git", "rev-parse", "HEAD"], stdout="old_sha\n"),
        ]
    )

    result = run_upgrade(
        prefix=prefix,
        target_sha="new_sha",
        backup_path=tmp_path / "backup_dir",
        dry_run=True,
        runner=runner,
        smoke_runner=runner,
    )

    argvs = [call.args[0] for call in runner.call_args_list]
    # Only the pre-flight git rev-parse ran.
    assert argvs == [["git", "rev-parse", "HEAD"]], (
        f"dry-run must NOT call any mutating subprocess; argvs={argvs!r}"
    )
    # Result still records every phase so the operator can audit.
    assert result.phases_completed == [
        PHASE_FETCH,
        PHASE_CHECKOUT,
        PHASE_UV_SYNC,
        PHASE_SIGN_CATALOG,
        PHASE_RESTART,
        PHASE_SMOKE,
    ]
    # DRY-RUN markers hit stderr.
    captured = capsys.readouterr()
    assert "[DRY-RUN]" in captured.err
    assert "git fetch" in captured.err


# ---------------------------------------------------------------------------
# Tests 11-14: verify_install_smoke pass / fail / warn / non-zero exit
# ---------------------------------------------------------------------------


def test_verify_install_smoke_passes_on_clean_report(tmp_path: Path) -> None:
    """Clean report (fail=0, warn=0) returns the parsed dict."""
    runner = MagicMock(
        return_value=_ok_completed(
            ["verify-install.sh"],
            stdout=json.dumps(
                {
                    "checks": [{"name": "binaries.python3", "status": "ok", "detail": "3.12"}],
                    "summary": {"ok": 1, "warn": 0, "fail": 0},
                }
            ),
        )
    )

    report = verify_install_smoke(
        prefix=tmp_path / "prefix",
        config_dir=tmp_path / "etc",
        state_dir=tmp_path / "var-lib",
        log_dir=tmp_path / "log",
        user="nora",
        runner=runner,
    )

    assert report["summary"] == {"ok": 1, "warn": 0, "fail": 0}
    # Runner was called with --json --strict and the resolved paths.
    argv = runner.call_args.args[0]
    assert "--json" in argv
    assert "--strict" in argv
    assert "--user" in argv
    assert argv[argv.index("--user") + 1] == "nora"


def test_verify_install_smoke_raises_on_fail(tmp_path: Path) -> None:
    """``summary.fail > 0`` raises ``SmokeFailed`` carrying the report."""
    runner = MagicMock(
        return_value=_ok_completed(
            ["verify-install.sh"],
            stdout=json.dumps(
                {
                    "checks": [{"name": "binaries.python3", "status": "fail", "detail": "missing"}],
                    "summary": {"ok": 0, "warn": 0, "fail": 1},
                }
            ),
        )
    )

    with pytest.raises(SmokeFailed) as excinfo:
        verify_install_smoke(
            prefix=tmp_path / "prefix",
            config_dir=tmp_path / "etc",
            state_dir=tmp_path / "var-lib",
            log_dir=tmp_path / "log",
            user="nora",
            runner=runner,
        )
    assert excinfo.value.report["summary"]["fail"] == 1


def test_verify_install_smoke_raises_on_warn(tmp_path: Path) -> None:
    """``summary.warn > 0`` raises ``SmokeFailed`` (strict mode)."""
    runner = MagicMock(
        return_value=_ok_completed(
            ["verify-install.sh"],
            stdout=json.dumps(
                {
                    "checks": [{"name": "binaries.python3", "status": "warn", "detail": "old"}],
                    "summary": {"ok": 0, "warn": 1, "fail": 0},
                }
            ),
        )
    )

    with pytest.raises(SmokeFailed) as excinfo:
        verify_install_smoke(
            prefix=tmp_path / "prefix",
            config_dir=tmp_path / "etc",
            state_dir=tmp_path / "var-lib",
            log_dir=tmp_path / "log",
            user="nora",
            runner=runner,
        )
    assert excinfo.value.report["summary"]["warn"] == 1


def test_verify_install_smoke_raises_on_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero exit raises ``SmokeFailed`` with stderr in the message."""
    runner = MagicMock(
        return_value=subprocess.CompletedProcess(
            args=["verify-install.sh"],
            returncode=1,
            stdout="",
            stderr="verify-install.sh: missing config",
        )
    )

    with pytest.raises(SmokeFailed) as excinfo:
        verify_install_smoke(
            prefix=tmp_path / "prefix",
            config_dir=tmp_path / "etc",
            state_dir=tmp_path / "var-lib",
            log_dir=tmp_path / "log",
            user="nora",
            runner=runner,
        )
    assert "verify-install.sh: missing config" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Tests 15-20: dispatcher integration (`nora upgrade`)
# ---------------------------------------------------------------------------


def test_nora_upgrade_dispatches_to_upgrade_module(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`nora upgrade --ref v0.3.8 --dry-run` calls run_upgrade with target_sha=v0.3.8.

    Mirrors ``test_nora_prompt_sync_invokes_sync_prompts`` —
    monkeypatches ``nora.cli.main`` so the dispatcher never boots the
    MCP server, and stubs ``nora.upgrade.run_upgrade`` so we can
    assert the exact kwargs.
    """
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod
    from nora import upgrade as upgrade_mod

    captured_kwargs: dict[str, Any] = {}

    def fake_run_upgrade(**kwargs: Any) -> UpgradeResult:
        captured_kwargs.update(kwargs)
        return UpgradeResult(
            preflight=PreFlightResult(
                noop=False, current_sha="old", target_sha=kwargs.get("target_sha", "new")
            ),
            backup_path=kwargs.get("backup_path"),
            phases_completed=[PHASE_FETCH, PHASE_SMOKE],
            final_sha=kwargs.get("target_sha", "new"),
            smoke_report={"summary": {"ok": 1, "warn": 0, "fail": 0}},
        )

    def fake_create_backup(**kwargs: Any) -> Path:
        return tmp_path / "backup_20260922T121530Z"

    monkeypatch.setattr(upgrade_mod, "run_upgrade", fake_run_upgrade)
    monkeypatch.setattr(upgrade_mod, "create_backup", fake_create_backup)
    monkeypatch.setattr(
        upgrade_mod,
        "preflight",
        lambda **kwargs: PreFlightResult(noop=False, current_sha="old", target_sha="v0.3.8"),
    )

    rc = main_mod.main(["upgrade", "--ref", "v0.3.8", "--dry-run"])

    assert rc == 0
    assert captured_kwargs.get("target_sha") == "v0.3.8"
    assert captured_kwargs.get("dry_run") is True
    # Backwards-compat: backup was created and forwarded.
    assert captured_kwargs.get("backup_path") == tmp_path / "backup_20260922T121530Z"


def test_nora_upgrade_noop_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Preflight no-op (already at target) exits 0 without calling run_upgrade."""
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod
    from nora import upgrade as upgrade_mod

    run_called = {"value": False}

    def fake_run_upgrade(**kwargs: Any) -> UpgradeResult:  # pragma: no cover - should not fire
        run_called["value"] = True
        return UpgradeResult(
            preflight=PreFlightResult(noop=True, current_sha="x", target_sha="x"),
            backup_path=None,
            phases_completed=[],
            final_sha="x",
            smoke_report=None,
        )

    monkeypatch.setattr(
        upgrade_mod,
        "preflight",
        lambda **kwargs: PreFlightResult(
            noop=True, current_sha="abc", target_sha="abc", reason="already at target"
        ),
    )
    monkeypatch.setattr(upgrade_mod, "run_upgrade", fake_run_upgrade)

    rc = main_mod.main(["upgrade"])

    assert rc == 0
    assert run_called["value"] is False
    captured = capsys.readouterr()
    assert "already at abc" in captured.err


def test_nora_upgrade_check_only_short_circuits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--check-only`` prints the preflight result and exits 0 without run_upgrade."""
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod
    from nora import upgrade as upgrade_mod

    run_called = {"value": False}

    def fake_run_upgrade(**kwargs: Any) -> UpgradeResult:  # pragma: no cover - should not fire
        run_called["value"] = True
        return UpgradeResult(
            preflight=PreFlightResult(noop=False, current_sha="a", target_sha="b"),
            backup_path=None,
            phases_completed=[],
            final_sha="b",
            smoke_report=None,
        )

    monkeypatch.setattr(
        upgrade_mod,
        "preflight",
        lambda **kwargs: PreFlightResult(noop=False, current_sha="aaa", target_sha="bbb"),
    )
    monkeypatch.setattr(upgrade_mod, "run_upgrade", fake_run_upgrade)

    rc = main_mod.main(["upgrade", "--check-only"])

    assert rc == 0
    assert run_called["value"] is False
    captured = capsys.readouterr()
    assert "check-only" in captured.err
    assert "bbb" in captured.err


def test_nora_upgrade_no_backup_skips_backup(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``--no-backup`` skips create_backup and passes backup_path=None to run_upgrade."""
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod
    from nora import upgrade as upgrade_mod

    captured_kwargs: dict[str, Any] = {}

    def fake_run_upgrade(**kwargs: Any) -> UpgradeResult:
        captured_kwargs.update(kwargs)
        return UpgradeResult(
            preflight=PreFlightResult(noop=False, current_sha="a", target_sha="b"),
            backup_path=kwargs.get("backup_path"),
            phases_completed=[],
            final_sha="b",
            smoke_report={"summary": {"ok": 1, "warn": 0, "fail": 0}},
        )

    backup_called = {"value": False}

    def fake_create_backup(**kwargs: Any) -> Path:
        backup_called["value"] = True
        return tmp_path / "backup"

    monkeypatch.setattr(
        upgrade_mod,
        "preflight",
        lambda **kwargs: PreFlightResult(noop=False, current_sha="a", target_sha="b"),
    )
    monkeypatch.setattr(upgrade_mod, "run_upgrade", fake_run_upgrade)
    monkeypatch.setattr(upgrade_mod, "create_backup", fake_create_backup)

    rc = main_mod.main(["upgrade", "--no-backup"])

    assert rc == 0
    assert backup_called["value"] is False
    assert captured_kwargs.get("backup_path") is None
    captured = capsys.readouterr()
    assert "backup:" not in captured.err


def test_nora_upgrade_json_output_emits_valid_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--json`` emits machine-readable JSON to stdout."""
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod
    from nora import upgrade as upgrade_mod

    def fake_run_upgrade(**kwargs: Any) -> UpgradeResult:
        return UpgradeResult(
            preflight=PreFlightResult(noop=False, current_sha="a", target_sha="v0.3.8"),
            backup_path=kwargs.get("backup_path"),
            phases_completed=[PHASE_FETCH, PHASE_SMOKE],
            final_sha="v0.3.8",
            smoke_report={"summary": {"ok": 1, "warn": 0, "fail": 0}},
        )

    def fake_create_backup(**kwargs: Any) -> Path:
        return Path("/var/lib/nora/upgrades/20260922T121530Z")

    monkeypatch.setattr(upgrade_mod, "run_upgrade", fake_run_upgrade)
    monkeypatch.setattr(upgrade_mod, "create_backup", fake_create_backup)
    monkeypatch.setattr(
        upgrade_mod,
        "preflight",
        lambda **kwargs: PreFlightResult(noop=False, current_sha="a", target_sha="v0.3.8"),
    )

    rc = main_mod.main(["upgrade", "--json", "--dry-run", "--ref", "v0.3.8"])

    assert rc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out.strip())
    assert isinstance(payload, dict)
    assert payload["final_sha"] == "v0.3.8"
    assert "phases_completed" in payload
    assert "preflight" in payload
    assert payload["preflight"]["target_sha"] == "v0.3.8"


def test_nora_upgrade_failed_phase_returns_nonzero(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """``UpgradeFailed`` surfaces on stderr with the failing phase + backup path."""
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod
    from nora import upgrade as upgrade_mod

    backup_path = tmp_path / "backup"

    def fake_run_upgrade(**kwargs: Any) -> UpgradeResult:
        raise UpgradeFailed(
            phase="uv_sync",
            backup_path=backup_path,
            message="uv sync failed",
        )

    monkeypatch.setattr(upgrade_mod, "run_upgrade", fake_run_upgrade)
    monkeypatch.setattr(
        upgrade_mod,
        "preflight",
        lambda **kwargs: PreFlightResult(noop=False, current_sha="a", target_sha="v0.3.8"),
    )
    monkeypatch.setattr(
        upgrade_mod,
        "create_backup",
        lambda **kwargs: tmp_path / "backup_20260922T121530Z",
    )

    rc = main_mod.main(["upgrade", "--ref", "v0.3.8"])

    assert rc == 1
    captured = capsys.readouterr()
    assert "uv_sync" in captured.err
    assert str(backup_path) in captured.err
    assert "uv sync failed" in captured.err


# ---------------------------------------------------------------------------
# Bonus test: dispatcher + upgrade module — env file parser helper.
# ---------------------------------------------------------------------------


def test_source_env_file_parses_quoted_values(tmp_path: Path) -> None:
    """``_source_env_file`` strips surrounding quotes + ignores comments / blanks.

    Smoke test for the small helper that feeds ``sign_catalog.py`` so
    an operator with quoted values in ``/etc/nora/nora.env`` still
    gets the right ``NORA_OID_CATALOG_SIGNING_KEY`` propagated.
    """
    env_file = tmp_path / "nora.env"
    env_file.write_text(
        "# top-level comment\n"
        "\n"
        'NORA_OID_CATALOG_SIGNING_KEY="abc123"\n'
        "NORA_OTHER='plain'\n"
        "INVALID_LINE_NO_EQUALS\n"
        "NORA_USER=nora\n",
        encoding="utf-8",
    )

    parsed = _source_env_file(tmp_path)

    assert parsed["NORA_OID_CATALOG_SIGNING_KEY"] == "abc123"
    assert parsed["NORA_OTHER"] == "plain"
    assert parsed["NORA_USER"] == "nora"
    assert "INVALID_LINE_NO_EQUALS" not in parsed
