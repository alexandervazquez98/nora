"""Tests for `scripts/verify-install.sh`.

The verifier is the day-2 guardrail: it answers "is my install healthy?"
without needing the operator to read the entire OPERATIONS.md
troubleshooting table. The tests below cover:

1. **Static checks** — script exists, is executable, passes `bash -n`,
   lists every documented flag in `--help`.

2. **Behavioural checks** — the verifier produces JSON output in the
   documented schema, fails when `signing_key` is world-readable, warns
   when `nora.env` is group-readable, and exits 2 with `--strict` on any
   WARN.

3. **Source-level invariants** — the verifier embeds the canonical tool
   + prompt names so a renamed tool without a verifier update surfaces
   here, the JSON-RPC probe uses the documented protocolVersion, and
   the script never echoes the signing key.

The functional probe (`check_functional`) is exercised end-to-end via
`--skip-functional` so the tests never need `sudo` or a real `nora-mcp`.
A separate coverage note in OPERATIONS.md describes the live probe.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.installer.conftest import run_script, verify_args_for

# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------


def test_verify_script_exists_and_is_executable(verify_script: Path) -> None:
    """verify-install.sh MUST exist and be executable."""
    assert verify_script.exists()
    assert os.access(verify_script, os.X_OK), (
        f"verify-install.sh not executable; chmod +x {verify_script}"
    )


def test_verify_script_passes_bash_n(verify_script: Path) -> None:
    """`bash -n` MUST pass — parseable shell syntax."""
    completed = subprocess.run(
        ["bash", "-n", str(verify_script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, (
        f"bash -n failed on verify-install.sh: stderr={completed.stderr!r}"
    )


# Flags the verifier MUST register per the spec.
REQUIRED_VERIFY_FLAGS = (
    "--json",
    "--strict",
    "--skip-functional",
    "--skip-systemd",
)


def test_verify_help_exits_zero_and_lists_required_flags(
    verify_script: Path,
) -> None:
    """`--help` exits 0 and prints every required flag."""
    result = run_script(verify_script, "--help", timeout=10)
    assert result.returncode == 0, (
        f"--help must exit 0; got {result.returncode}, stderr={result.stderr!r}"
    )
    combined = result.combined
    for flag in REQUIRED_VERIFY_FLAGS:
        assert flag in combined, f"--help output missing required flag {flag!r}; got:\n{combined}"


def test_verify_unknown_flag_fails_loud(verify_script: Path) -> None:
    """An unknown flag exits 2 and mentions the offending flag."""
    result = run_script(verify_script, "--no-such-flag", timeout=10)
    assert result.returncode == 2, (
        f"unknown flag must exit 2; got {result.returncode}, stderr={result.stderr!r}"
    )
    assert "--no-such-flag" in result.combined, (
        f"stderr must mention the offending flag; got: {result.combined!r}"
    )
    assert "FAIL" in result.combined, (
        f"stderr must include a [FAIL] prefix; got: {result.combined!r}"
    )


# ---------------------------------------------------------------------------
# JSON output schema
# ---------------------------------------------------------------------------


def test_verify_json_output_is_valid_json(
    verify_script: Path,
    fake_install,
    venv_python_on_path: Path,
    fake_systemctl: Path,
) -> None:
    """`--json` MUST emit a parseable JSON object with `checks` + `summary`."""
    args = [
        "--json",
        "--skip-functional",
        "--skip-systemd",
        *verify_args_for(fake_install),
    ]
    result = run_script(verify_script, *args, timeout=20)
    # All checks pass → exit 0; failure surfaces as a parse error below.
    assert result.returncode == 0, (
        f"verify with a clean fake install must exit 0; got "
        f"{result.returncode}, stderr={result.stderr!r}"
    )
    # The JSON is on stdout; the human-readable table goes to stderr.
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict), f"top-level JSON must be a dict; got {type(payload)}"
    assert "checks" in payload, "JSON must include `checks` key"
    assert "summary" in payload, "JSON must include `summary` key"
    assert isinstance(payload["checks"], list), "`checks` must be a list"
    assert isinstance(payload["summary"], dict), "`summary` must be a dict"
    for entry in payload["checks"]:
        assert set(entry.keys()) == {"name", "status", "detail"}, (
            f"each check entry must have name/status/detail; got {entry.keys()}"
        )
        assert entry["status"] in {"ok", "warn", "fail"}, (
            f"unknown status {entry['status']!r} in {entry}"
        )
    summary = payload["summary"]
    assert set(summary.keys()) == {"ok", "warn", "fail"}, (
        f"summary must have ok/warn/fail; got {summary.keys()}"
    )


# ---------------------------------------------------------------------------
# Source-level invariants
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def verify_script_source(verify_script: Path) -> str:
    """The raw text of verify-install.sh, shared by the source tests."""
    return verify_script.read_text()


def test_verify_checks_known_tool_names_and_prompt_names(
    verify_script_source: str,
) -> None:
    """The 5 tool names + 2 prompt names MUST be embedded in the verifier.

    If a tool is renamed in the codebase without updating the verifier,
    the functional probe will FAIL on every install — that's the alarm
    bell this test installs.
    """
    tools = (
        "snmp_get_pmp450i_radio_metrics",
        "search_intervention_history",
        "get_device_lifecycle_summary",
        "correlate_sector_interference",
        "save_intervention_record",
    )
    prompts = ("netops_orchestrator", "snmp_pmp450i")
    for name in (*tools, *prompts):
        assert name in verify_script_source, (
            f"verify-install.sh must mention the canonical name {name!r}"
        )


def test_verify_functional_check_uses_jsonrpc_protocol_version_2025_06_18(
    verify_script_source: str,
) -> None:
    """The functional probe MUST handshake with protocolVersion 2025-06-18.

    The MCP protocol version is part of the smoke-test contract in
    INSTALL.md; the verifier must speak the same version so an operator
    sees consistent results across the smoke test and the verify run.
    """
    assert "2025-06-18" in verify_script_source, (
        "verify-install.sh must handshake with protocolVersion '2025-06-18'"
    )


def test_verify_does_not_echo_signing_key(verify_script_source: str) -> None:
    """`printf`/`echo` MUST NEVER print the raw signing key.

    Same invariants as install.sh — see the install test for the mask
    pattern whitelist. The verifier is the day-2 guardrail; a leak in
    its output would defeat the secret hygiene the install script
    establishes.
    """
    bad: list[tuple[int, str]] = []
    for idx, line in enumerate(verify_script_source.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not (("printf" in stripped) or ("echo" in stripped)):
            continue
        if not (("${KEY" in stripped) or ("${key" in stripped)):
            continue
        if (
            "..." in stripped
            or ":0:" in stripped
            or ": -" in stripped
            or "mask" in stripped.lower()
        ):
            continue
        bad.append((idx, line))

    assert not bad, "verify-install.sh may echo the signing key on these lines:\n" + "\n".join(
        f"  line {n}: {line_text}" for n, line_text in bad
    )


# ---------------------------------------------------------------------------
# Permission check behaviours
# ---------------------------------------------------------------------------


def test_verify_perm_check_fails_when_signing_key_world_readable(
    verify_script: Path,
    fake_install_signing_key_world_readable,
    venv_python_on_path: Path,
    fake_systemctl: Path,
) -> None:
    """`signing_key` mode 0644 → FAIL + exit 1 + stderr mentions the file."""
    args = [
        *verify_args_for(fake_install_signing_key_world_readable),
        "--skip-functional",
        "--skip-systemd",
    ]
    result = run_script(verify_script, *args, timeout=20)
    assert result.returncode == 1, (
        f"signing_key mode 0644 must FAIL with exit 1; got "
        f"{result.returncode}, stderr={result.stderr!r}"
    )
    assert "FAIL" in result.combined, (
        f"stderr must include a [FAIL] prefix; got: {result.combined!r}"
    )
    assert "signing_key" in result.combined, (
        f"stderr must mention signing_key; got: {result.combined!r}"
    )


def test_verify_perm_check_warns_on_nora_env_644(
    verify_script: Path,
    fake_install_env_file_644,
    venv_python_on_path: Path,
    fake_systemctl: Path,
) -> None:
    """`nora.env` mode 0644 → WARN, exit 0 by default, exit 2 with --strict."""
    base_args = [
        *verify_args_for(fake_install_env_file_644),
        "--skip-functional",
        "--skip-systemd",
    ]

    # Default mode: WARN is OK, exit 0.
    default_result = run_script(verify_script, *base_args, timeout=20)
    assert default_result.returncode == 0, (
        f"nora.env mode 0644 with default mode must exit 0; got "
        f"{default_result.returncode}, stderr={default_result.stderr!r}"
    )
    assert "WARN" in default_result.combined, (
        f"stderr must include a [WARN] prefix; got: {default_result.combined!r}"
    )
    assert "nora.env" in default_result.combined, (
        f"stderr must mention nora.env; got: {default_result.combined!r}"
    )

    # --strict mode: WARN must turn into a non-zero exit (2).
    strict_result = run_script(verify_script, "--strict", *base_args, timeout=20)
    assert strict_result.returncode == 2, (
        f"nora.env mode 0644 with --strict must exit 2; got "
        f"{strict_result.returncode}, stderr={strict_result.stderr!r}"
    )


def test_verify_strict_exits_nonzero_on_warn(
    verify_script: Path,
    fake_install_env_file_644,
    venv_python_on_path: Path,
    fake_systemctl: Path,
) -> None:
    """--strict MUST convert any WARN into a non-zero exit code.

    This is the canonical "any-soft-failure-is-a-hard-failure" mode that
    CI pipelines use. Locking the behaviour here prevents a future
    refactor from accidentally demoting the WARN band.
    """
    args = [
        "--strict",
        *verify_args_for(fake_install_env_file_644),
        "--skip-functional",
        "--skip-systemd",
    ]
    result = run_script(verify_script, *args, timeout=20)
    assert result.returncode == 2, (
        f"--strict on a WARN scenario must exit 2; got {result.returncode}, "
        f"stderr={result.stderr!r}"
    )


def test_verify_clean_install_with_strict_exits_zero(
    verify_script: Path,
    fake_install,
    venv_python_on_path: Path,
    fake_systemctl: Path,
) -> None:
    """A clean install with --strict MUST still exit 0 (nothing to escalate)."""
    args = [
        "--strict",
        *verify_args_for(fake_install),
        "--skip-functional",
        "--skip-systemd",
    ]
    result = run_script(verify_script, *args, timeout=20)
    assert result.returncode == 0, (
        f"clean install + --strict must exit 0; got {result.returncode}, stderr={result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# ERR trap firing
# ---------------------------------------------------------------------------


def test_verify_trap_fires_on_failure(
    verify_script: Path,
) -> None:
    """The ERR trap MUST be installed and reference $LINENO.

    Same invariant as install.sh — a missing trap turns a partial
    failure into a confusing non-zero exit with no actionable message.
    """
    source = verify_script.read_text()
    assert "trap" in source, "verify-install.sh must install a trap"
    assert "LINENO" in source, "trap must reference $LINENO"
    assert "failed at line" in source, "trap message must include 'failed at line'"
