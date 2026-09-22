"""Tests for ``nora.doctor`` + the ``nora doctor`` dispatcher.

Issue #60 / PR-3 — render ``scripts/verify-install.sh --json`` output
as a human-readable health check + JSON for machine consumers, plus
a ``net.ipv4.ping_group_range`` persistence cross-check (D10).

Patterns (mirror ``tests/test_upgrade.py``):

* The subprocess injection seam (``verify_runner``, ``sysctl_runner``,
  ``fs_runner``) is stubbed per-test with ``MagicMock`` so no real
  ``git``, ``sysctl``, or ``Path.read_text`` call ever fires.
* The ``nora doctor`` dispatcher tests follow the ``test_nora_prompt_sync_*``
  pattern: monkeypatch the doctor module functions and skip the MCP
  boot path with
  ``monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)``.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nora.doctor import (
    DEFAULT_CONFIG_DIR,
    DEFAULT_PREFIX,
    EXPECTED_PING_RANGE,
    DoctorCheck,
    DoctorReport,
    VerifyInstallFailed,
    _check_sysctl_persistence,
    _run_verify_install_json,
    _verify_install_script_path,
    render_human,
    render_json,
    run_doctor,
)

# ---------------------------------------------------------------------------
# Helpers — fake subprocess + filesystem runners used by tests.
# ---------------------------------------------------------------------------


def _ok_completed(
    args: list[str],
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> subprocess.CompletedProcess[str]:
    """Build a ``CompletedProcess`` mirroring ``subprocess.run(check=False)``."""
    return subprocess.CompletedProcess(
        args=args, returncode=returncode, stdout=stdout, stderr=stderr
    )


def _verify_json_runner(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> MagicMock:
    """Build a MagicMock that returns one ``CompletedProcess`` from a verify call."""
    runner = MagicMock()
    runner.return_value = _ok_completed(
        args=["verify-stub"],
        stdout=stdout,
        stderr=stderr,
        returncode=returncode,
    )
    return runner


# ---------------------------------------------------------------------------
# Tests 1: DoctorCheck is a frozen dataclass.
# ---------------------------------------------------------------------------


def test_doctor_check_is_frozen_dataclass() -> None:
    """``DoctorCheck`` rejects attribute mutation after construction.

    The dataclass is ``frozen=True`` so audit-log writers / JSON
    serialisers can rely on the fields being immutable post-build.
    """
    check = DoctorCheck(
        name="binaries.python3",
        status="ok",
        detail="Python 3.12.14",
        source="verify_install",
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        check.name = "something_else"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Tests 2-4: _run_verify_install_json — parsing, exit, malformed stdout.
# ---------------------------------------------------------------------------


def test_run_verify_install_json_parses_valid_report() -> None:
    """Happy path: the runner emits valid JSON; the function returns the dict.

    The verify runner is a ``MagicMock`` that returns one
    ``CompletedProcess`` with the canonical ``{"checks": [...],
    "summary": {...}}`` payload. Asserts the parsed dict equals the
    input AND that the runner was called with ``check=False`` (so
    the dispatcher never raises on non-zero exit codes that are
    semantically OK at the JSON layer).
    """
    expected = {
        "checks": [
            {"name": "binaries.python3", "status": "ok", "detail": "Python 3.12.14"},
            {"name": "binaries.uv", "status": "ok", "detail": "uv 0.4.18"},
        ],
        "summary": {"ok": 2, "warn": 0, "fail": 0},
    }
    runner = _verify_json_runner(stdout=json.dumps(expected))

    result = _run_verify_install_json(runner=runner)

    assert result == expected
    # ``check=False`` is critical — non-zero exits must NOT bubble up
    # as ``CalledProcessError`` because the doctor surfaces those via
    # the report summary, not as exceptions.
    runner.assert_called_once()
    kwargs = runner.call_args.kwargs
    assert kwargs.get("check") is False
    assert kwargs.get("capture_output") is True
    assert kwargs.get("text") is True


def test_run_verify_install_json_raises_on_nonzero_exit() -> None:
    """Non-zero exit surfaces as ``VerifyInstallFailed`` carrying the script's stderr.

    The dispatcher uses this branch to print a clear stderr message;
    the script's stderr is preserved on the exception so the
    operator can grep the audit log.
    """
    runner = _verify_json_runner(
        stdout="some non-json output",
        stderr="verify-install.sh: missing prefix",
        returncode=2,
    )

    with pytest.raises(VerifyInstallFailed) as excinfo:
        _run_verify_install_json(runner=runner)

    assert "rc=2" in str(excinfo.value)
    assert "verify-install.sh: missing prefix" in str(excinfo.value)
    assert excinfo.value.stderr == "verify-install.sh: missing prefix"


def test_run_verify_install_json_raises_on_malformed_stdout() -> None:
    """Empty / non-JSON ``stdout`` surfaces as ``VerifyInstallFailed``.

    The script returning exit 0 with junk on stdout is a contract
    break (the verifier is supposed to ALWAYS emit valid JSON in
    ``--json`` mode). The doctor surfaces the parse error so the
    operator can investigate the shell script — not a swallowed
    warning that would let a wedged verifier pass silently.
    """
    runner = _verify_json_runner(stdout="not json{")

    with pytest.raises(VerifyInstallFailed) as excinfo:
        _run_verify_install_json(runner=runner)

    msg = str(excinfo.value)
    assert "parseable JSON" in msg
    assert "JSONDecodeError" in msg or "ValueError" in msg


# ---------------------------------------------------------------------------
# Tests 5-8: _check_sysctl_persistence — OK / warn-missing /
# warn-default / fail-tampered.
# ---------------------------------------------------------------------------


def test_check_sysctl_persistence_ok_when_file_and_runtime_agree() -> None:
    """Both file AND runtime report the expected value → OK with the canonical detail."""
    file_contents = "# nora sysctl persistence\nnet.ipv4.ping_group_range = 0 2147483647\n"
    sysctl_runner = MagicMock()
    sysctl_runner.return_value = _ok_completed(
        args=["sysctl", "-n", "net.ipv4.ping_group_range"],
        stdout=f"{EXPECTED_PING_RANGE}\n",
    )
    fs_runner = MagicMock(return_value=file_contents)

    check = _check_sysctl_persistence(sysctl_runner=sysctl_runner, fs_runner=fs_runner)

    assert isinstance(check, DoctorCheck)
    assert check.status == "ok"
    assert check.source == "sysctl_persistence"
    assert EXPECTED_PING_RANGE in check.detail
    assert "99-nora.conf" in check.detail


def test_check_sysctl_persistence_warn_when_file_missing() -> None:
    """File missing (FileNotFoundError) AND runtime correct → WARN with naming the file."""
    sysctl_runner = MagicMock()
    sysctl_runner.return_value = _ok_completed(
        args=["sysctl", "-n", "net.ipv4.ping_group_range"],
        stdout=f"{EXPECTED_PING_RANGE}\n",
    )

    def fs_runner(_path: str) -> str:
        raise FileNotFoundError(_path)

    check = _check_sysctl_persistence(sysctl_runner=sysctl_runner, fs_runner=fs_runner)

    assert check.status == "warn"
    assert "99-nora.conf" in check.detail
    assert "missing" in check.detail


def test_check_sysctl_persistence_warn_when_runtime_default() -> None:
    """File present with expected value BUT runtime reports default `1 0` → WARN.

    The detail MUST mention the default value and the
    didn't-reboot-didn't-re-apply diagnostic so the operator
    understands the most common install-after-reboot failure
    mode.
    """
    file_contents = "net.ipv4.ping_group_range = 0 2147483647\n"
    sysctl_runner = MagicMock()
    sysctl_runner.return_value = _ok_completed(
        args=["sysctl", "-n", "net.ipv4.ping_group_range"],
        stdout="1 0\n",
    )
    fs_runner = MagicMock(return_value=file_contents)

    check = _check_sysctl_persistence(sysctl_runner=sysctl_runner, fs_runner=fs_runner)

    assert check.status == "warn"
    assert "1 0" in check.detail
    assert "reboot" in check.detail or "re-apply" in check.detail


def test_check_sysctl_persistence_fail_when_file_has_wrong_value() -> None:
    """File present with a value other than `0 2147483647` → FAIL carrying the bad value.

    The detail MUST include the captured bad value so the operator
    sees exactly what is wrong (vs an opaque "file present but
    wrong" message that would require re-reading the file).
    """
    file_contents = "net.ipv4.ping_group_range = 100 200\n"
    sysctl_runner = MagicMock()
    sysctl_runner.return_value = _ok_completed(
        args=["sysctl", "-n", "net.ipv4.ping_group_range"],
        stdout="whatever\n",
    )
    fs_runner = MagicMock(return_value=file_contents)

    check = _check_sysctl_persistence(sysctl_runner=sysctl_runner, fs_runner=fs_runner)

    assert check.status == "fail"
    assert "100 200" in check.detail


# ---------------------------------------------------------------------------
# Tests 9-10: run_doctor — merging + verify-install-failure fallback.
# ---------------------------------------------------------------------------


def test_run_doctor_merges_verify_and_sysctl_checks() -> None:
    """A clean verifier run delivers 2 verify checks + 1 sysctl check → 3 total."""
    verify_payload = {
        "checks": [
            {"name": "binaries.python3", "status": "ok", "detail": "Python 3.12.14"},
            {"name": "binaries.uv", "status": "ok", "detail": "uv 0.4.18"},
        ],
        "summary": {"ok": 2, "warn": 0, "fail": 0},
    }
    verify_runner = _verify_json_runner(stdout=json.dumps(verify_payload))
    sysctl_runner = MagicMock()
    sysctl_runner.return_value = _ok_completed(
        args=["sysctl", "-n", "net.ipv4.ping_group_range"],
        stdout=f"{EXPECTED_PING_RANGE}\n",
    )
    # Inject the file-seam so the sysctl cross-check sees a
    # well-formed persistence file rather than whatever the host
    # filesystem happens to contain. Mirrors ``install.sh``'s
    # output verbatim.
    fs_runner = MagicMock(return_value=f"net.ipv4.ping_group_range = {EXPECTED_PING_RANGE}\n")

    report = run_doctor(
        verify_runner=verify_runner,
        sysctl_runner=sysctl_runner,
        fs_runner=fs_runner,
    )

    assert isinstance(report, DoctorReport)
    assert len(report.checks) == 3
    assert report.ok_count == 3
    assert report.warn_count == 0
    assert report.fail_count == 0
    # sysctl_persistence is the LAST row (stable ordering).
    assert report.checks[-1].name == "sysctl_persistence"
    # First two rows preserve verify-install execution order.
    assert report.checks[0].name == "binaries.python3"
    assert report.checks[1].name == "binaries.uv"


def test_run_doctor_handles_verify_install_failure_with_sysctl_fallback() -> None:
    """Verifier fails (exit 2) AND sysctl is OK → report has BOTH a sysctl OK + verify fail row.

    The operator must always see SOMETHING when even the verifier
    is wedged. The synthetic ``verify_install.run`` row carries
    the error message in its detail so the cause is still visible.
    """
    verify_runner = _verify_json_runner(stdout="broken", stderr="kaboom", returncode=2)
    sysctl_runner = MagicMock()
    sysctl_runner.return_value = _ok_completed(
        args=["sysctl", "-n", "net.ipv4.ping_group_range"],
        stdout=f"{EXPECTED_PING_RANGE}\n",
    )
    fs_runner = MagicMock(return_value=f"net.ipv4.ping_group_range = {EXPECTED_PING_RANGE}\n")

    report = run_doctor(
        verify_runner=verify_runner,
        sysctl_runner=sysctl_runner,
        fs_runner=fs_runner,
    )

    assert isinstance(report, DoctorReport)
    assert len(report.checks) == 2
    sysctl_rows = [c for c in report.checks if c.source == "sysctl_persistence"]
    verify_rows = [c for c in report.checks if c.name == "verify_install.run"]
    assert len(sysctl_rows) == 1
    assert sysctl_rows[0].status == "ok"
    assert len(verify_rows) == 1
    assert verify_rows[0].status == "fail"
    assert "kaboom" in verify_rows[0].detail or "rc=2" in verify_rows[0].detail
    # The synthetic fail counts toward fail_count so CI gates trip.
    assert report.fail_count == 1


# ---------------------------------------------------------------------------
# Tests 11-12: render_human — every check + summary footer.
# ---------------------------------------------------------------------------


def test_render_human_includes_every_check() -> None:
    """Every check appears in the table by name, with uppercase status + detail substring."""
    report = DoctorReport(
        checks=[
            DoctorCheck(
                name="binaries.python3",
                status="ok",
                detail="Python 3.12.14",
                source="verify_install",
            ),
            DoctorCheck(
                name="binaries.uv",
                status="warn",
                detail="uv 0.4.18 (>=0.4)",
                source="verify_install",
            ),
            DoctorCheck(
                name="sysctl_persistence",
                status="fail",
                detail="bad value",
                source="sysctl_persistence",
            ),
        ]
    )

    output = render_human(report)

    assert "binaries.python3" in output
    assert "binaries.uv" in output
    assert "sysctl_persistence" in output
    # Status must be UPPERCASE so the table reads at a glance.
    assert "OK" in output
    assert "WARN" in output
    assert "FAIL" in output
    # Detail substring check.
    assert "Python 3.12.14" in output
    assert "uv 0.4.18 (>=0.4)" in output
    assert "bad value" in output


def test_render_human_includes_summary_counts() -> None:
    """Footer line ``ok=N warn=N fail=N`` is present and matches the report counts."""
    report = DoctorReport(
        checks=[
            DoctorCheck(name="a", status="ok", detail="x", source="verify_install"),
            DoctorCheck(name="b", status="warn", detail="x", source="verify_install"),
            DoctorCheck(name="c", status="fail", detail="x", source="verify_install"),
        ]
    )

    output = render_human(report)
    assert "ok=1" in output
    assert "warn=1" in output
    assert "fail=1" in output


# ---------------------------------------------------------------------------
# Test 13: render_json round-trips.
# ---------------------------------------------------------------------------


def test_render_json_round_trips() -> None:
    """JSON render → parse → checks length + summary match the report counts."""
    report = DoctorReport(
        checks=[
            DoctorCheck(
                name="binaries.python3",
                status="ok",
                detail="Python 3.12.14",
                source="verify_install",
            ),
            DoctorCheck(
                name="binaries.uv",
                status="warn",
                detail="uv 0.4.18",
                source="verify_install",
            ),
            DoctorCheck(
                name="sysctl_persistence",
                status="fail",
                detail="tampered",
                source="sysctl_persistence",
            ),
        ]
    )

    raw = render_json(report)
    parsed = json.loads(raw)

    assert len(parsed["checks"]) == len(report.checks)
    assert parsed["summary"] == {
        "ok": report.ok_count,
        "warn": report.warn_count,
        "fail": report.fail_count,
    }
    # Stable ordering: sysctl_persistence is the LAST row.
    assert parsed["checks"][-1]["name"] == "sysctl_persistence"


# ---------------------------------------------------------------------------
# Tests 14-19: `nora doctor` dispatcher.
# ---------------------------------------------------------------------------


def _stub_run_doctor(monkeypatch: pytest.MonkeyPatch, report: DoctorReport) -> dict[str, object]:
    """Replace ``nora.doctor.run_doctor`` with a stub that returns the given report."""
    from nora import doctor as doctor_mod

    captured: dict[str, object] = {}
    real_render_human = doctor_mod.render_human
    real_render_json = doctor_mod.render_json

    def fake_run_doctor(**_kwargs: object) -> DoctorReport:
        captured["called"] = True
        return report

    monkeypatch.setattr(doctor_mod, "run_doctor", fake_run_doctor)
    # Keep the real renderers in scope so dispatcher tests assert on
    # their actual output (not a stubbed one).
    captured["render_human"] = real_render_human
    captured["render_json"] = real_render_json
    return captured


def test_nora_doctor_dispatches_to_doctor_module(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``nora doctor`` exits 0 on a clean report AND emits ``render_human`` on stderr."""
    report = DoctorReport(
        checks=[
            DoctorCheck(
                name="binaries.python3",
                status="ok",
                detail="Python 3.12.14",
                source="verify_install",
            )
        ]
    )
    _stub_run_doctor(monkeypatch, report)
    # Skip the MCP boot path so the dispatcher never tries to launch
    # FastMCP in the test process.
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod

    rc = main_mod.main(["doctor"])

    captured = capsys.readouterr()
    assert rc == 0
    # Human table goes to STDERR; stdout stays empty so piping to
    # awk / jq stays clean.
    assert "binaries.python3" in captured.err
    assert captured.out == ""


def test_nora_doctor_json_flag_emits_json_on_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``nora doctor --json`` emits JSON to stdout AND renders to stderr (parallel)."""
    report = DoctorReport(
        checks=[
            DoctorCheck(
                name="binaries.python3",
                status="ok",
                detail="Python 3.12.14",
                source="verify_install",
            )
        ]
    )
    _stub_run_doctor(monkeypatch, report)
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod

    rc = main_mod.main(["doctor", "--json"])

    captured = capsys.readouterr()
    assert rc == 0
    # JSON shape on stdout; parsed cleanly.
    parsed = json.loads(captured.out.strip())
    assert parsed["checks"][0]["name"] == "binaries.python3"
    # The parallel stderr render is for human visibility when
    # running interactively with --json (CI scripts can ignore it).
    assert "binaries.python3" in captured.err


def test_nora_doctor_strict_exits_2_on_warn(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``nora doctor --strict`` exits 2 on a WARN-only report (no FAIL)."""
    report = DoctorReport(
        checks=[
            DoctorCheck(
                name="binaries.python3",
                status="warn",
                detail="old python",
                source="verify_install",
            )
        ]
    )
    _stub_run_doctor(monkeypatch, report)
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod

    rc = main_mod.main(["doctor", "--strict"])

    assert rc == 2
    capsys.readouterr()  # drain noise


def test_nora_doctor_default_exits_1_on_fail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``nora doctor`` (no ``--strict``) exits 1 on a single FAIL."""
    report = DoctorReport(
        checks=[
            DoctorCheck(
                name="binaries.git",
                status="fail",
                detail="git not on PATH",
                source="verify_install",
            )
        ]
    )
    _stub_run_doctor(monkeypatch, report)
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod

    rc = main_mod.main(["doctor"])

    assert rc == 1
    capsys.readouterr()  # drain noise


def test_nora_doctor_default_exits_0_on_warn_alone(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``nora doctor`` (no ``--strict``) exits 0 on a WARN-only report.

    WARN alone is NOT a fail-closed signal — the operator upgraded
    intentionally knowing that. Only ``--strict`` upgrades WARN to
    exit 2.
    """
    report = DoctorReport(
        checks=[
            DoctorCheck(
                name="permissions.nora.env",
                status="warn",
                detail="group-readable; tighten to 0640",
                source="verify_install",
            )
        ]
    )
    _stub_run_doctor(monkeypatch, report)
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod

    rc = main_mod.main(["doctor"])

    assert rc == 0
    capsys.readouterr()  # drain noise


def test_nora_doctor_verify_install_failure_still_emits_partial_report(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``nora doctor`` exits 1 on ``VerifyInstallFailed`` AND stderr names the failure.

    The stub raises ``VerifyInstallFailed`` to mirror a wedged
    verify-install.sh. The dispatcher MUST emit a clear stderr
    message naming the failure so the operator can grep the audit
    log.
    """
    from nora import doctor as doctor_mod

    def fake_run_doctor(**_kwargs: object) -> DoctorReport:
        raise doctor_mod.VerifyInstallFailed(
            stderr="verify-install.sh: hard fail",
            message="verify-install.sh exited rc=2 — hard fail",
        )

    monkeypatch.setattr(doctor_mod, "run_doctor", fake_run_doctor)
    monkeypatch.setattr("nora.cli.main", lambda *a, **kw: None)

    from nora import __main__ as main_mod

    rc = main_mod.main(["doctor"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "verify-install.sh" in captured.err


# ---------------------------------------------------------------------------
# Helper sanity test — make sure the script-path helper resolves under prefix.
# ---------------------------------------------------------------------------


def test_verify_install_script_path_resolves_under_prefix(
    tmp_path: Path,
) -> None:
    """``_verify_install_script_path`` joins ``prefix / 'scripts' / 'verify-install.sh'``."""
    prefix = tmp_path / "opt" / "nora"
    resolved = _verify_install_script_path(prefix)
    assert resolved == prefix / "scripts" / "verify-install.sh"


# ---------------------------------------------------------------------------
# Sanity check — default path constants match install.sh / upgrade.py.
# ---------------------------------------------------------------------------


def test_default_paths_match_install_sh() -> None:
    """DEFAULT_PREFIX / DEFAULT_CONFIG_DIR must match the install.sh / upgrade.py defaults.

    Stops a future rename of install.sh's prefix from silently
    breaking ``nora doctor`` on stock installs.
    """
    assert DEFAULT_PREFIX == Path("/opt/nora")
    assert DEFAULT_CONFIG_DIR == Path("/etc/nora")
