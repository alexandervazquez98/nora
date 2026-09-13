"""Shared pytest fixtures for the installer test suite.

The installer tests do NOT execute the real install (no root, no systemd,
no `useradd` available on the macOS test env). They exercise:

* `bash -n` static syntax checks.
* `--help` / `--version` introspection via subprocess.
* Dry-run with `--skip-*` flags so the script's read-only phases
  (prereq, user check, summary) run against the host environment without
  touching the filesystem or starting systemd.
* Fabricated `tmp_path` filesystem layouts that simulate the install
  shape, so the verify script's permission + functional checks fire
  against predictable inputs.
* Path overrides (`--prefix`, `--config-dir`, ...) so the verifier does
  not try to look at `/opt/nora` on a dev machine.

The `clean_env` fixture strips any `NORA_*` env vars from the parent
process so a developer's local config does not leak into a test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPT_DIR = PROJECT_ROOT / "scripts"

# Env vars the NORA runtime uses — strip them so a developer's local
# state does not leak into a test invocation. Match the keys that show up
# in `.env.example` plus the prompt dir.
_NORA_ENV_KEYS = (
    "NORA_OID_CATALOG_SIGNING_KEY",
    "NORA_OID_CATALOGS_PATH",
    "NORA_DEVICES_INVENTORY_PATH",
    "NORA_INTERVENTIONS_DIR",
    "NORA_PROMPTS_DIR",
    "NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS",
    "NORA_INTERVENTIONS_CORRELATE_SCAN_LIMIT",
)


@dataclass
class ScriptResult:
    """Lightweight wrapper around `subprocess.CompletedProcess`.

    Mirrors the fields the tests actually inspect. Returning a typed
    object beats a raw `CompletedProcess` because the field names here
    are stable across Python versions (the dataclass's `text` matches
    CPython's `CompletedProcess.text`).
    """

    returncode: int
    stdout: str
    stderr: str
    cmd: list[str]

    @property
    def combined(self) -> str:
        """stdout + stderr concatenated — handy for substring checks."""
        return f"{self.stdout}\n{self.stderr}"


# ---------------------------------------------------------------------------
# Path + script fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def project_root() -> Path:
    """The NORA repo root (the directory that contains `scripts/`)."""
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def script_dir() -> Path:
    """`scripts/` — where the installer + verifier live."""
    return SCRIPT_DIR


@pytest.fixture(scope="session")
def install_script(script_dir: Path) -> Path:
    """Absolute path to `scripts/install.sh`. Asserts it exists and is executable."""
    p = script_dir / "install.sh"
    assert p.exists(), f"install.sh missing at {p}"
    assert os.access(p, os.X_OK), f"install.sh not executable: {p}"
    return p


@pytest.fixture(scope="session")
def verify_script(script_dir: Path) -> Path:
    """Absolute path to `scripts/verify-install.sh`. Asserts it exists and is executable."""
    p = script_dir / "verify-install.sh"
    assert p.exists(), f"verify-install.sh missing at {p}"
    assert os.access(p, os.X_OK), f"verify-install.sh not executable: {p}"
    return p


# ---------------------------------------------------------------------------
# Subprocess runner — wraps `bash <script> <args>` for the tests.
# ---------------------------------------------------------------------------


def run_script(
    path: Path,
    *args: str,
    env: dict[str, str] | None = None,
    cwd: Path | str | None = None,
    timeout: int = 30,
) -> ScriptResult:
    """Invoke a shell script via `bash` (no `shell=True`).

    Captures stdout + stderr separately so the tests can assert on either
    stream. Raises nothing — call sites inspect `returncode` themselves.
    """
    full_env = os.environ.copy() if env is None else env
    completed = subprocess.run(
        ["bash", str(path), *args],
        check=False,
        capture_output=True,
        text=True,
        env=full_env,
        cwd=cwd,
        timeout=timeout,
    )
    return ScriptResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
        cmd=["bash", str(path), *args],
    )


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Return a sanitized env dict + apply it via monkeypatch.

    Strips every `NORA_*` variable from the host environment so a
    developer's local settings cannot influence a test. Use the
    returned dict as the `env=` arg to `run_script` for hermetic
    invocations.
    """
    env = {k: v for k, v in os.environ.items() if k not in _NORA_ENV_KEYS}
    monkeypatch.setenv("PATH", env.get("PATH", ""))
    for key in _NORA_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return env


@pytest.fixture
def venv_python_on_path(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Prepend `.venv/bin` to PATH so `python3` resolves to Python 3.12+.

    The test host (macOS) ships only Python 3.9 at `/usr/bin/python3`,
    which trips the installer / verifier's prereq check. The project
    venv at `<repo>/.venv/bin/python` is 3.12. This fixture exposes that
    Python as the bare `python3` for tests that need the prereq to pass.
    """
    venv_bin = PROJECT_ROOT / ".venv" / "bin"
    if not venv_bin.exists():
        pytest.skip(f"Project venv missing at {venv_bin}; cannot set up Python 3.12 PATH")
    # Symlink: .venv/bin/python3 -> .venv/bin/python. We do this so the
    # `python3` shim follows the same interpreter that pytest is using.
    shim = venv_bin / "python3"
    if not shim.exists():
        try:
            os.symlink("python", shim)
        except OSError:
            # Fall back to a wrapper script that execs `python`.
            shim.write_text('#!/bin/sh\nexec "$(dirname "$0")/python" "$@"\n')
            shim.chmod(0o755)
    new_path = f"{venv_bin}{os.pathsep}{os.environ.get('PATH', '')}"
    monkeypatch.setenv("PATH", new_path)
    return venv_bin


# ---------------------------------------------------------------------------
# Fake systemctl — lets the verify checks pass on macOS where systemctl
# is absent.
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_systemctl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put a fake `systemctl` in PATH that reports version 250 + active state.

    The fake only handles the two `systemctl` subcommands the verifier
    uses: `--version` (returns a stable banner) and `is-active nora-mcp`
    (returns `active`). All other invocations return non-zero so a test
    that forgets to pass `--skip-systemd` will FAIL loud instead of
    silently passing on the fake.
    """
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir(exist_ok=True)
    script = fakebin / "systemctl"
    script.write_text(
        "#!/bin/sh\n"
        "# Fake systemctl for installer tests. Supports:\n"
        "#   --version                 -> prints systemd banner\n"
        "#   is-active nora-mcp        -> prints active\n"
        "# Anything else returns non-zero so misuse surfaces immediately.\n"
        'case "$1" in\n'
        "    --version)\n"
        "        echo 'systemd 250 (fake for installer tests)'\n"
        "        exit 0\n"
        "        ;;\n"
        "    is-active)\n"
        '        if [ "$2" = "nora-mcp" ]; then\n'
        "            echo active\n"
        "            exit 0\n"
        "        fi\n"
        "        echo unknown\n"
        "        exit 1\n"
        "        ;;\n"
        "esac\n"
        'echo "fake-systemctl: unsupported: $*" >&2\n'
        "exit 99\n"
    )
    script.chmod(0o755)
    new_path = f"{fakebin}{os.pathsep}{os.environ.get('PATH', '')}"
    monkeypatch.setenv("PATH", new_path)
    return fakebin


# ---------------------------------------------------------------------------
# Fake install layout — for the verify tests.
# ---------------------------------------------------------------------------


@dataclass
class FakeInstall:
    """A complete fake install tree under `tmp_path`."""

    root: Path
    prefix: Path
    config_dir: Path
    state_dir: Path
    log_dir: Path
    nora_mcp: Path
    env_file: Path
    signing_key: Path
    user: str


def _build_fake_install(
    tmp_path: Path,
    *,
    signing_key_mode: int = 0o600,
    env_file_mode: int = 0o640,
) -> FakeInstall:
    """Build a fake install tree; helper for the `fake_install` fixture."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "root"

    root = tmp_path / "install"
    prefix = root / "opt" / "nora"
    config_dir = root / "etc" / "nora"
    state_dir = root / "var" / "lib" / "nora"
    log_dir = root / "var" / "log" / "nora"

    config_dir.mkdir(parents=True, exist_ok=True)
    state_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "interventions").mkdir(parents=True, exist_ok=True)

    prefix.mkdir(parents=True, exist_ok=True)
    bin_dir = prefix / ".venv" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    nora_mcp = bin_dir / "nora-mcp"
    # The fake nora-mcp is a no-op stub — when the verifier runs the
    # functional check it will be spawned via `sudo -u ...`, but the
    # verify tests that exercise paths + perms use --skip-functional
    # so the stub never gets to actually run.
    nora_mcp.write_text("#!/bin/sh\nexit 0\n")
    nora_mcp.chmod(0o755)

    signing_key = config_dir / "signing_key"
    signing_key.write_text("fake-signing-key-for-test-only\n")
    signing_key.chmod(signing_key_mode)

    env_file = config_dir / "nora.env"
    env_file.write_text(
        "# fake nora.env for installer tests\n"
        "NORA_OID_CATALOG_SIGNING_KEY=fake\n"
        "NORA_OID_CATALOGS_PATH=/tmp/nope\n"
        "NORA_DEVICES_INVENTORY_PATH=/tmp/nope.yaml\n"
        "NORA_INTERVENTIONS_DIR=/tmp/nope-interventions\n"
    )
    env_file.chmod(env_file_mode)

    return FakeInstall(
        root=root,
        prefix=prefix,
        config_dir=config_dir,
        state_dir=state_dir,
        log_dir=log_dir,
        nora_mcp=nora_mcp,
        env_file=env_file,
        signing_key=signing_key,
        user=user,
    )


@pytest.fixture
def fake_install(tmp_path: Path) -> FakeInstall:
    """A complete fake install tree with all checks expected to pass.

    signing_key is 0600, nora.env is 0640, both owned by the current
    user (via the `--user` override in the test invocation). Use this
    fixture as a baseline; the `fake_install_*` variants below tweak
    specific attributes.
    """
    return _build_fake_install(tmp_path)


@pytest.fixture
def fake_install_signing_key_world_readable(tmp_path: Path) -> FakeInstall:
    """Variant where `signing_key` has mode 0644 — verify must FAIL."""
    return _build_fake_install(tmp_path, signing_key_mode=0o644)


@pytest.fixture
def fake_install_env_file_644(tmp_path: Path) -> FakeInstall:
    """Variant where `nora.env` has mode 0644 — verify must WARN."""
    return _build_fake_install(tmp_path, env_file_mode=0o644)


# ---------------------------------------------------------------------------
# Convenience: build the flag list for a verify invocation.
# ---------------------------------------------------------------------------


def verify_args_for(fake: FakeInstall) -> list[str]:
    """Return the `--prefix/--config-dir/--state-dir/--log-dir/--user` flags."""
    return [
        "--prefix",
        str(fake.prefix),
        "--config-dir",
        str(fake.config_dir),
        "--state-dir",
        str(fake.state_dir),
        "--log-dir",
        str(fake.log_dir),
        "--user",
        fake.user,
    ]


# Re-export SimpleNamespace for tests that need a generic bag of attrs.
__all__ = [
    "FakeInstall",
    "PROJECT_ROOT",
    "ScriptResult",
    "SCRIPT_DIR",
    "clean_env",
    "fake_install",
    "fake_install_env_file_644",
    "fake_install_signing_key_world_readable",
    "fake_systemctl",
    "install_script",
    "project_root",
    "run_script",
    "venv_python_on_path",
    "verify_args_for",
    "verify_script",
]


# `shutil` is imported so future fixtures that copy tree slices can use
# it without re-importing. Keep the import to make that dependency explicit.
_ = shutil
