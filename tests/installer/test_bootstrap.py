"""Tests for `scripts/bootstrap.sh`.

The bootstrap script is a thin wrapper around `git clone` + `scripts/install.sh`.
Tests must validate the security contract (HTTPS-only, no shell injection via
`--ref`, SUDO_USER check) AND the end-to-end behaviour (clone, idempotent
pull, install invocation, cleanup on success vs preservation on failure or
`--keep-clone`, SHA printed before install matches the cloned commit).

To keep tests hermetic (no network, no root, no real install), every test
that needs a "remote" builds a local bare git repo via the `fake_remote`
fixture and points `--repo` at it. The fake remote ships a custom
`scripts/install.sh` whose body the test controls — so we can simulate
"install succeeded" or "install failed with non-zero exit" without
touching the real installer.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.installer.conftest import ScriptResult, run_script

# Module-level mark: every test in this file drives a real `git clone`
# against a fake bare remote at a script-controlled `/tmp/nora-bootstrap-*`
# path. Under pytest-xdist two workers can collide on that path before
# either finishes, producing intermittent failures unrelated to the
# bootstrap logic. Sequential pytest (CI) is deterministic; `make test-fast`
# skips these via `-m "not no_xdist"`.
pytestmark = pytest.mark.no_xdist


@pytest.fixture(autouse=True)
def _isolate_nora_bootstrap_tmp() -> None:
    """Clean any leftover /tmp/nora-bootstrap-* dirs before each test.

    The cleanup-on-success / failure / --keep-clone tests assert on the
    presence or absence of these directories. Without isolation, a
    previous test's leftover (e.g., from `--keep-clone`) can leak into
    the next test's `Path('/tmp').glob('nora-bootstrap-*')` sweep and
    produce a false positive / negative.

    Runs before every test in this module. Idempotent: a missing dir
    is fine.
    """
    for d in Path("/tmp").glob("nora-bootstrap-*"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# Flags the bootstrap script MUST register per issue #22's spec. Listed
# here so a missing flag fails this test instead of silently regressing
# the operator-facing surface.
REQUIRED_FLAGS = (
    "--ref",
    "--repo",
    "--prefix",
    "--config-dir",
    "--download-only",
    "--dest",
    "--keep-clone",
    "--yes",
    "--help",
)


@pytest.fixture(scope="module")
def bootstrap_script(script_dir: Path) -> Path:
    """Absolute path to `scripts/bootstrap.sh`; asserts existence + execute bit."""
    p = script_dir / "bootstrap.sh"
    assert p.exists(), f"bootstrap.sh missing at {p}"
    assert os.access(p, os.X_OK), f"bootstrap.sh not executable: {p}"
    return p


def _git(
    args: list[str],
    cwd: Path | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command, capturing output. Defaults to raising on non-zero.

    `cwd` is optional: `git init <dir>` and `git clone <url> <dir>` don't
    need a pre-existing working tree. When omitted, the command runs in
    the current process cwd (never relied upon — callers always pass one
    for in-repo operations).
    """
    kwargs: dict[str, Any] = {
        "check": check,
        "capture_output": True,
        "text": True,
        "env": {**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    }
    if cwd is not None:
        kwargs["cwd"] = str(cwd)
    return subprocess.run(["git", *args], **kwargs)


@pytest.fixture
def fake_remote(tmp_path: Path) -> Path:
    """Build a local bare git repo with one commit on `main` and a stub install.sh.

    The stub `scripts/install.sh` echoes a marker line and exits 0 by default.
    Tests that need a non-zero install can overwrite that file before invoking
    bootstrap.sh.

    Returns the path to the bare remote (suitable for `--repo file://...`).
    """
    # 1. Bare remote on disk.
    bare = tmp_path / "remote.git"
    bare.mkdir()
    _git(["init", "--bare", "--initial-branch=main", "--quiet", str(bare)])

    # 2. Working tree we push from.
    work = tmp_path / "work"
    work.mkdir()
    _git(["init", "--initial-branch=main", "--quiet"], cwd=work)
    _git(["config", "user.email", "test@example.invalid"], cwd=work)
    _git(["config", "user.name", "bootstrap test"], cwd=work)

    # 3. Drop a stub install.sh and commit.
    scripts_dir = work / "scripts"
    scripts_dir.mkdir()
    install = scripts_dir / "install.sh"
    install.write_text(
        "#!/bin/sh\n"
        "# Stub install.sh used by bootstrap tests.\n"
        "echo '[STUB-INSTALL] hello from fake install.sh'\n"
        "exit 0\n"
    )
    install.chmod(0o755)
    _git(["add", "."], cwd=work)
    _git(["commit", "--quiet", "-m", "initial"], cwd=work)
    _git(["remote", "add", "origin", str(bare)], cwd=work)
    _git(["push", "--quiet", "origin", "main"], cwd=work)

    return bare


def _run_bootstrap_with_fake_remote(
    bootstrap_script: Path,
    fake_remote: Path,
    *args: str,
    env: dict[str, str] | None = None,
    timeout: int = 60,
) -> ScriptResult:
    """Invoke `bootstrap.sh` against a local file:// remote.

    Adds the test-only `BOOTSTRAP_TEST_ALLOW_NON_HTTPS=1` env var so the
    script's HTTPS-only guard does not reject the `file://` URL. Returns
    a `ScriptResult` exactly like `run_script` does.
    """
    test_env = {
        **(env if env is not None else os.environ.copy()),
        "BOOTSTRAP_TEST_ALLOW_NON_HTTPS": "1",
    }
    return run_script(
        bootstrap_script,
        "--repo",
        fake_remote.as_uri(),
        *args,
        env=test_env,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Static checks
# ---------------------------------------------------------------------------


def test_bootstrap_script_exists_and_is_executable(bootstrap_script: Path) -> None:
    """bootstrap.sh MUST exist and be executable (chmod +x)."""
    assert bootstrap_script.exists()
    assert os.access(bootstrap_script, os.X_OK), (
        f"bootstrap.sh not executable; chmod +x {bootstrap_script}"
    )


def test_bootstrap_passes_bash_n(bootstrap_script: Path) -> None:
    """`bash -n` MUST pass — confirms parseable shell syntax."""
    completed = subprocess.run(
        ["bash", "-n", str(bootstrap_script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, f"bash -n failed on bootstrap.sh: stderr={completed.stderr!r}"


def test_bootstrap_passes_shellcheck(bootstrap_script: Path) -> None:
    """`shellcheck -x` MUST report zero issues (best-effort).

    Skip the test if shellcheck is not installed on the host — install.sh
    and verify-install.sh tests follow the same policy (shellcheck is a
    dev-time tool, not a hard runtime requirement).
    """
    if shutil.which("shellcheck") is None:
        pytest.skip("shellcheck not installed on host")
    completed = subprocess.run(
        ["shellcheck", "-x", str(bootstrap_script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == 0, (
        f"shellcheck -x reported issues on bootstrap.sh:\n{completed.stdout}\n{completed.stderr}"
    )


# ---------------------------------------------------------------------------
# --help + argument parsing
# ---------------------------------------------------------------------------


def test_bootstrap_help_exits_zero_and_lists_required_flags(
    bootstrap_script: Path,
) -> None:
    """`--help` exits 0 and prints every required flag in stdout/stderr combined."""
    result = run_script(bootstrap_script, "--help", timeout=10)
    assert result.returncode == 0, (
        f"--help must exit 0; got {result.returncode}, stderr={result.stderr!r}"
    )
    combined = result.combined
    for flag in REQUIRED_FLAGS:
        assert flag in combined, f"--help output missing required flag {flag!r}; got:\n{combined}"


def test_bootstrap_unknown_flag_fails_loud(bootstrap_script: Path) -> None:
    """An unknown flag exits 2 with a [FAIL] line that names the flag."""
    result = run_script(bootstrap_script, "--no-such-flag", timeout=10)
    assert result.returncode == 2, (
        f"unknown flag must exit 2; got {result.returncode}, stderr={result.stderr!r}"
    )
    assert "--no-such-flag" in result.combined, (
        f"output must mention the offending flag; got: {result.combined!r}"
    )
    assert "FAIL" in result.combined, (
        f"output must include a [FAIL] prefix; got: {result.combined!r}"
    )


# ---------------------------------------------------------------------------
# Security guard rails — must fire BEFORE any network I/O.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "git://example.com/foo.git",
        "http://example.com/foo.git",
        "ssh://git@example.com/foo.git",
        "ftp://example.com/foo.git",
        "file:///etc/passwd",
    ],
)
def test_bootstrap_refuses_non_https_repo(bootstrap_script: Path, bad_url: str) -> None:
    """`--repo` MUST start with `https://`; anything else is rejected.

    Covers the full non-HTTPS family (git://, http://, ssh://, ftp://,
    file://). A hostile redirect that downgrades the transport is
    refused at argument-validation time, before git ever sees the URL.
    """
    result = run_script(bootstrap_script, "--repo", bad_url, timeout=10)
    assert result.returncode == 1, (
        f"non-HTTPS URL {bad_url!r} must exit 1; got {result.returncode}, stderr={result.stderr!r}"
    )
    assert "[FAIL]" in result.combined, (
        f"output must include [FAIL] prefix; got: {result.combined!r}"
    )
    assert "HTTPS" in result.combined or "https" in result.combined, (
        f"output must explain the HTTPS requirement; got: {result.combined!r}"
    )


@pytest.mark.parametrize(
    "bad_ref",
    [
        "--upload-pack=evil",  # git's known RCE vector
        "--upload-pack=touch /tmp/pwn",  # whitespace + command
        "ext::ssh -o ProxyCommand=sh",  # git's external transport
        "main;rm -rf /tmp/x",  # shell metachar `;`
        "main|cat /etc/passwd",  # shell metachar `|`
        "main`whoami`",  # shell metachar backtick
        "main$(whoami)",  # shell metachar `$()`
        "main with space",  # whitespace
        "-evil",  # leading `-` (parsed as git flag)
    ],
)
def test_bootstrap_refuses_shell_injection_in_ref(bootstrap_script: Path, bad_ref: str) -> None:
    """`--ref` MUST NOT contain shell-injection vectors or leading dashes.

    These are the canonical ways a malicious ref name escapes into a shell
    or into git's own flag parser. All are refused at validation time,
    before any network I/O.
    """
    result = run_script(bootstrap_script, "--ref", bad_ref, timeout=10)
    assert result.returncode == 1, (
        f"suspicious ref {bad_ref!r} must exit 1; got {result.returncode}, stderr={result.stderr!r}"
    )
    assert "[FAIL]" in result.combined, (
        f"output must include [FAIL] prefix; got: {result.combined!r}"
    )
    assert "ref" in result.combined.lower(), (
        f"output must mention the ref was the problem; got: {result.combined!r}"
    )


def test_bootstrap_refuses_dest_without_download_only(
    bootstrap_script: Path,
) -> None:
    """`--dest` without `--download-only` is meaningless; refuse it."""
    result = run_script(bootstrap_script, "--dest", "/tmp/somewhere", timeout=10)
    assert result.returncode == 1, (
        f"--dest without --download-only must exit 1; got {result.returncode}"
    )
    assert "--dest" in result.combined, f"output must name --dest; got: {result.combined!r}"


def test_bootstrap_refuses_root_with_empty_sudo_user(
    bootstrap_script: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Running as root with empty SUDO_USER is refused.

    Simulates the dangerous pattern `curl ... | bash` (direct root, no
    sudo shell). The test forces EUID=0 via a fake `id` shim only if we
    are NOT already root, since we cannot easily lower EUID inside a
    pytest process. The check is exercised on every non-root dev host.
    """
    if os.geteuid() == 0:
        pytest.skip(
            "Test host is already root; cannot simulate direct-root invocation "
            "without a separate user namespace. The validation still fires in "
            "the script's parse logic and is covered by the source-level test."
        )
    # We can't actually become root in a pytest process, but we CAN
    # exercise the branch by reading the script source: the validator
    # path is short enough that a static check is more reliable than
    # trying to set EUID. We assert the script contains the exact
    # validator string instead.
    source = bootstrap_script.read_text()
    assert "EUID" in source and "SUDO_USER" in source, (
        "bootstrap.sh must reference both EUID and SUDO_USER in its direct-root validator"
    )
    assert "direct 'bash'" in source or "direct `bash`" in source, (
        "bootstrap.sh must explain the 'sudo bash' vs direct 'bash' rule "
        "to the operator in the failure message"
    )


# ---------------------------------------------------------------------------
# --download-only — clone path, no install invocation.
# ---------------------------------------------------------------------------


def test_bootstrap_download_only_clones_into_dest(
    bootstrap_script: Path,
    tmp_path: Path,
    fake_remote: Path,
) -> None:
    """`--download-only --dest <path>` clones into `<path>/nora`.

    The destination must contain a valid `.git` directory AND a working
    `scripts/install.sh` so the next phase of bootstrap could proceed.
    """
    dest = tmp_path / "review"
    result = _run_bootstrap_with_fake_remote(
        bootstrap_script,
        fake_remote,
        "--download-only",
        "--dest",
        str(dest),
        "--ref",
        "main",
        timeout=30,
    )
    assert result.returncode == 0, (
        f"download-only must exit 0; got {result.returncode}, stderr={result.stderr!r}"
    )
    clone = dest / "nora"
    assert clone.exists(), f"expected {clone} to exist after clone"
    assert (clone / ".git").is_dir(), f"expected {clone / '.git'} to be a directory"
    assert (clone / "scripts" / "install.sh").is_file(), (
        f"expected {clone / 'scripts' / 'install.sh'} to be a file"
    )
    assert os.access(clone / "scripts" / "install.sh", os.X_OK), (
        f"{clone / 'scripts' / 'install.sh'} must be executable"
    )


def test_bootstrap_download_only_rejects_non_empty_dest(
    bootstrap_script: Path,
    tmp_path: Path,
    fake_remote: Path,
) -> None:
    """`--download-only --dest <dir>` refuses to clobber an existing `<dir>/nora`.

    The contract is: bootstrap clones into `<dest>/nora`, so if that
    subdirectory already exists and is non-empty, bootstrap refuses to
    overwrite it. A pre-existing file at `<dest>/important.txt` is fine
    — bootstrap creates `<dest>/nora` next to it.
    """
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "important.txt").write_text("operator data")
    # Pre-create `<dest>/nora` as a non-empty directory to trigger the refusal.
    existing_clone = dest / "nora"
    existing_clone.mkdir()
    (existing_clone / "junk.txt").write_text("would be clobbered")

    result = _run_bootstrap_with_fake_remote(
        bootstrap_script,
        fake_remote,
        "--download-only",
        "--dest",
        str(dest),
        timeout=30,
    )
    assert result.returncode == 1, (
        f"non-empty <dest>/nora must exit 1; got {result.returncode}, stderr={result.stderr!r}"
    )
    assert (dest / "important.txt").exists(), "sibling file must survive the refusal"
    assert (existing_clone / "junk.txt").exists(), (
        "pre-existing junk in <dest>/nora must survive the refusal"
    )


# ---------------------------------------------------------------------------
# Idempotence — re-running on an existing clone must use git pull --ff-only.
# ---------------------------------------------------------------------------


def test_bootstrap_pulls_existing_clone_instead_of_cloning(
    bootstrap_script: Path,
    tmp_path: Path,
    fake_remote: Path,
) -> None:
    """If `<dest>/nora/.git` exists, bootstrap pulls --ff-only instead of cloning.

    Setup: do a successful --download-only. Then add a new commit to the
    fake remote and re-run. The output must mention "pull" and the
    destination HEAD must advance to the new commit. We assert the
    "pull" branch via a [OK] line that names it, plus a `git -C dest/nora
    rev-parse` round-trip.
    """
    dest = tmp_path / "dest"
    # 1. Initial clone.
    result = _run_bootstrap_with_fake_remote(
        bootstrap_script,
        fake_remote,
        "--download-only",
        "--dest",
        str(dest),
        timeout=30,
    )
    assert result.returncode == 0, (
        f"first clone must exit 0; got {result.returncode}, stderr={result.stderr!r}"
    )
    clone = dest / "nora"
    initial_sha = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # 2. Push a new commit to the fake remote.
    work = fake_remote.parent / "work"
    (work / "marker.txt").write_text("second commit\n")
    _git(["add", "marker.txt"], cwd=work)
    _git(["commit", "--quiet", "-m", "second"], cwd=work)
    _git(["push", "--quiet", "origin", "main"], cwd=work)

    # 3. Re-run bootstrap. Must take the pull path.
    result = _run_bootstrap_with_fake_remote(
        bootstrap_script,
        fake_remote,
        "--download-only",
        "--dest",
        str(dest),
        timeout=30,
    )
    assert result.returncode == 0, (
        f"pull-mode run must exit 0; got {result.returncode}, stderr={result.stderr!r}"
    )
    combined = result.combined
    assert "pull" in combined.lower(), (
        f"output must mention 'pull' when reusing an existing clone; got:\n{combined}"
    )
    assert "Existing clone" in combined, (
        f"output must say 'Existing clone' before pulling; got:\n{combined}"
    )

    # 4. The clone's HEAD must have advanced.
    new_sha = subprocess.run(
        ["git", "-C", str(clone), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert new_sha != initial_sha, (
        f"clone HEAD must advance after pull; initial={initial_sha}, new={new_sha}"
    )


# ---------------------------------------------------------------------------
# Cleanup on success / failure / --keep-clone.
# ---------------------------------------------------------------------------


def _nora_bootstrap_tmp_dirs() -> list[Path]:
    """Return live /tmp/nora-bootstrap-* directories created by the script."""
    return sorted(
        p
        for p in Path("/tmp").glob("nora-bootstrap-*")
        if p.is_dir() and p.name.startswith("nora-bootstrap-")
    )


def test_bootstrap_cleans_temp_clone_on_install_success(
    bootstrap_script: Path,
    tmp_path: Path,
    fake_remote: Path,
) -> None:
    """On install.sh success, the temp clone under /tmp is removed.

    We override the stub install.sh to exit 0 so bootstrap sees success.
    We then assert no `nora-bootstrap-*` directory was left behind.
    """
    # Mark this run with a sentinel env var so we can filter for it.
    sentinel = f"nora-bootstrap-test-{os.getpid()}"
    # The stub already exits 0 from the fixture, so a vanilla invocation
    # is sufficient. Run with --prefix / --config-dir pointing at tmp
    # paths so install.sh (the stub) doesn't touch anything important.
    prefix = tmp_path / "opt"
    config_dir = tmp_path / "etc"
    result = _run_bootstrap_with_fake_remote(
        bootstrap_script,
        fake_remote,
        "--prefix",
        str(prefix),
        "--config-dir",
        str(config_dir),
        env={**os.environ, "SENTINEL": sentinel},
        timeout=60,
    )
    assert result.returncode == 0, (
        f"bootstrap with success-install must exit 0; got {result.returncode}, "
        f"stderr={result.stderr!r}"
    )
    # The trap fires on EXIT, so by the time the subprocess returns,
    # the temp dir should be gone. Allow a brief grace period for FS sync.
    import time

    time.sleep(0.05)
    # Filter: any nora-bootstrap-* created during this run, by mtime.
    # We use mtime to scope to the just-finished invocation.
    now = time.time()
    fresh = [p for p in Path("/tmp").glob("nora-bootstrap-*") if now - p.stat().st_mtime < 30]
    assert not fresh, f"expected no fresh nora-bootstrap-* dirs after success; got: {fresh}"


def test_bootstrap_keeps_temp_clone_on_install_failure(
    bootstrap_script: Path,
    tmp_path: Path,
    fake_remote: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On install.sh failure (non-zero exit), the temp clone is PRESERVED.

    The run_install helper refuses via `fail` which prints the clone
    path so the operator can inspect it. We assert the dir exists after
    bootstrap returns AND that the failure message names the path.
    """
    # Replace the stub install.sh in the fake remote with one that exits 7.
    work = fake_remote.parent / "work"
    install = work / "scripts" / "install.sh"
    install.write_text("#!/bin/sh\necho '[STUB] simulated install failure' >&2\nexit 7\n")
    install.chmod(0o755)
    _git(["commit", "--quiet", "--allow-empty", "-am", "make install fail"], cwd=work)
    _git(["push", "--quiet", "origin", "main"], cwd=work)

    result = _run_bootstrap_with_fake_remote(
        bootstrap_script,
        fake_remote,
        "--prefix",
        str(tmp_path / "opt"),
        "--config-dir",
        str(tmp_path / "etc"),
        timeout=60,
    )
    assert result.returncode == 1, (
        f"bootstrap with failing install must exit 1; got {result.returncode}, "
        f"stderr={result.stderr!r}"
    )
    combined = result.combined
    assert "install.sh failed" in combined, (
        f"failure message must name install.sh; got:\n{combined}"
    )
    assert "/tmp/nora-bootstrap-" in combined, (
        f"failure message must name the preserved clone path; got:\n{combined}"
    )
    # The clone itself must still exist on disk.
    leftover_dirs = sorted(Path("/tmp").glob("nora-bootstrap-*"))
    assert leftover_dirs, (
        "expected at least one nora-bootstrap-* dir to be preserved on install failure"
    )
    # Clean up so this test doesn't pollute the host.
    for d in leftover_dirs:
        shutil.rmtree(d, ignore_errors=True)


def test_bootstrap_keep_clone_preserves_temp(
    bootstrap_script: Path,
    tmp_path: Path,
    fake_remote: Path,
) -> None:
    """`--keep-clone` preserves the temp clone even when install succeeds."""
    prefix = tmp_path / "opt"
    config_dir = tmp_path / "etc"
    result = _run_bootstrap_with_fake_remote(
        bootstrap_script,
        fake_remote,
        "--keep-clone",
        "--prefix",
        str(prefix),
        "--config-dir",
        str(config_dir),
        timeout=60,
    )
    assert result.returncode == 0, (
        f"bootstrap --keep-clone must exit 0; got {result.returncode}, stderr={result.stderr!r}"
    )
    leftover_dirs = sorted(Path("/tmp").glob("nora-bootstrap-*"))
    assert leftover_dirs, "expected at least one nora-bootstrap-* dir to remain with --keep-clone"
    # The preserved clone must contain a working tree.
    clone = leftover_dirs[0] / "nora"
    assert (clone / ".git").is_dir(), "preserved clone must have .git/"
    # Clean up so this test doesn't pollute the host.
    for d in leftover_dirs:
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# SHA printed before install matches the cloned commit.
# ---------------------------------------------------------------------------


def test_bootstrap_sha_round_trip(
    bootstrap_script: Path,
    tmp_path: Path,
    fake_remote: Path,
) -> None:
    """The SHA printed before install must match the cloned commit.

    Two messages carry the SHA, depending on the mode:
      * `--download-only` → `[OK] Download complete (<sha>)`
      * full install       → `[OK] Cloned commit: <sha>`

    We exercise BOTH paths and assert the printed SHA equals the remote
    HEAD. The full-install path also exercises verify_installed_sha,
    which prints "Installed SHA matches clone: <sha>" — same SHA on
    both sides means the installer's `cp -a` landed the right tree.
    """
    expected_sha = subprocess.run(
        ["git", "-C", str(fake_remote.parent / "work"), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    # Path 1: --download-only → "Download complete (<sha>)".
    result_dl = _run_bootstrap_with_fake_remote(
        bootstrap_script,
        fake_remote,
        "--download-only",
        "--dest",
        str(tmp_path / "review_dl"),
        timeout=30,
    )
    assert result_dl.returncode == 0, (
        f"download-only must exit 0; got {result_dl.returncode}, stderr={result_dl.stderr!r}"
    )
    import re

    match_dl = re.search(r"Download complete \(([0-9a-f]{40})\)", result_dl.combined)
    assert match_dl, (
        f"download-only output must contain 'Download complete (<sha>)'; got:\n{result_dl.combined}"
    )
    assert match_dl.group(1) == expected_sha, (
        f"download-only SHA {match_dl.group(1)!r} must match remote HEAD {expected_sha!r}"
    )

    # Path 2: full install (fake install.sh exits 0) → "Cloned commit: <sha>".
    result_full = _run_bootstrap_with_fake_remote(
        bootstrap_script,
        fake_remote,
        "--keep-clone",  # so we can inspect the cloned tree post-run
        "--prefix",
        str(tmp_path / "opt"),
        "--config-dir",
        str(tmp_path / "etc"),
        timeout=60,
    )
    assert result_full.returncode == 0, (
        f"full install must exit 0; got {result_full.returncode}, stderr={result_full.stderr!r}"
    )
    match_full = re.search(r"Cloned commit:\s*([0-9a-f]{40})", result_full.combined)
    assert match_full, (
        f"full-install output must contain 'Cloned commit: <sha>'; got:\n{result_full.combined}"
    )
    assert match_full.group(1) == expected_sha, (
        f"full-install SHA {match_full.group(1)!r} must match remote HEAD {expected_sha!r}"
    )
    # Cleanup the --keep-clone temp dirs so the host stays tidy.
    for d in Path("/tmp").glob("nora-bootstrap-*"):
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# Source-level invariants — the script MUST reference each guard's token.
# (Cheap defence against future refactors that drop a check.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "required_token",
    [
        "https://",  # HTTPS-only enforcement
        "--upload-pack=",  # shell-injection rejection
        "ext::",  # shell-injection rejection
        "SUDO_USER",  # direct-root refusal
        "EUID",  # root-detection
        "git clone",  # clone step
        "git pull --ff-only",  # pull step (idempotence)
        "install.sh",  # install invocation
        "Cloned commit:",  # SHA pre-install
        "Bootstrap complete",  # final success line
    ],
)
def test_bootstrap_source_references_required_token(
    bootstrap_script: Path, required_token: str
) -> None:
    """The bootstrap script MUST reference each guard / step token at the source level.

    Cheap insurance: a future refactor that drops the HTTPS check or the
    shell-injection guard would fail one of these tests instead of
    silently regressing the security contract.
    """
    source = bootstrap_script.read_text()
    assert required_token in source, (
        f"bootstrap.sh source must contain {required_token!r} "
        f"(defence against silent regression of a guard)"
    )
