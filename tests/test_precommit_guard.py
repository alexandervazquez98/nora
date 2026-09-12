"""Tests for the pre-commit guard that blocks `.env` staging.

Spec scenario: secure-configuration Requirement "`.env` Is Never Tracked",
Scenario "accidental staging is rejected":

    GIVEN a developer runs `git add .env --force`
    WHEN the pre-commit hook runs
    THEN the commit is rejected with a clear message

Two layers are covered:

* **Regex pinning** (pure Python) — the regex `^(\\.env|.*/\\.env)$` MUST
  match `.env` and `<dir>/.env` but MUST NOT match `.env.example`,
  `.env.test`, `.env.local`, `.envrc`, etc.
* **End-to-end via the bash script** — the guard script (`scripts/
  check-no-env-staged.sh`) reads staged paths (from `git diff --cached`
  in production, from stdin in tests) and exits non-zero with a clear
  ERROR message when a `.env` is staged.
* **`.pre-commit-config.yaml` wiring** — the config MUST register a
  local hook with id `no-env-staging` running at the `pre-commit` stage.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HOOK_SCRIPT = PROJECT_ROOT / "scripts" / "check-no-env-staged.sh"
PRE_COMMIT_CONFIG = PROJECT_ROOT / ".pre-commit-config.yaml"

# The regex the hook uses. Pinned here so a tweak to the script regex
# cannot silently widen the match (e.g., start matching `.env.example`).
ENV_PATH_REGEX = re.compile(r"^(\.env|.*/\.env)$")


def _run_hook(stdin_text: str) -> subprocess.CompletedProcess[str]:
    """Run the guard script with a stdin feed of "staged" paths.

    The `--from-stdin` mode keeps the regex check testable without a
    real git index.
    """
    if not HOOK_SCRIPT.exists():
        raise AssertionError(
            f"Guard script missing: {HOOK_SCRIPT}. Create it before running pre-commit checks."
        )
    return subprocess.run(
        ["bash", str(HOOK_SCRIPT), "--from-stdin"],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=10,
    )


# --- Pure regex pinning ----------------------------------------------------


def test_regex_rejects_top_level_dotenv() -> None:
    """`.env` (no path prefix) MUST be flagged."""
    assert ENV_PATH_REGEX.fullmatch(".env") is not None, "Regex MUST match a top-level `.env` path"


def test_regex_rejects_nested_dotenv() -> None:
    """`<dir>/.env` (any directory prefix) MUST be flagged."""
    assert ENV_PATH_REGEX.fullmatch("path/to/.env") is not None, (
        "Regex MUST match a nested `<dir>/.env` path"
    )
    assert ENV_PATH_REGEX.fullmatch("a/b/c/.env") is not None, (
        "Regex MUST match deeply nested `.env` paths"
    )


def test_regex_allows_dotenv_example() -> None:
    """`.env.example` MUST NOT be flagged (the synthetic template is tracked)."""
    assert ENV_PATH_REGEX.fullmatch(".env.example") is None, (
        "Regex MUST NOT match `.env.example` — that is the tracked template"
    )


def test_regex_allows_dotenv_test() -> None:
    """`.env.test` MUST NOT be flagged."""
    assert ENV_PATH_REGEX.fullmatch(".env.test") is None, "Regex MUST NOT match `.env.test`"


def test_regex_allows_dotenv_local() -> None:
    """`.env.local` MUST NOT be flagged (a common sibling convention)."""
    assert ENV_PATH_REGEX.fullmatch(".env.local") is None, "Regex MUST NOT match `.env.local`"


def test_regex_allows_unrelated_dotenvrc() -> None:
    """`.envrc` is a different filename (direnv) — MUST NOT be flagged."""
    assert ENV_PATH_REGEX.fullmatch(".envrc") is None, (
        "Regex MUST NOT match `.envrc` (direnv convention)"
    )


def test_regex_allows_nested_dotenv_example() -> None:
    """`<dir>/.env.example` MUST NOT be flagged."""
    assert ENV_PATH_REGEX.fullmatch("docs/.env.example") is None, (
        "Regex MUST NOT match a nested `.env.example`"
    )


# --- End-to-end via the bash script ----------------------------------------


def test_script_rejects_dotenv_stdin() -> None:
    """Feeding `.env` to the script exits non-zero with a clear message."""
    proc = _run_hook(".env\n")
    assert proc.returncode == 1, (
        f"Guard script must exit 1 when `.env` is staged; "
        f"got returncode={proc.returncode}, stderr={proc.stderr!r}"
    )
    assert ".env" in proc.stderr, f"Stderr should mention `.env`; got: {proc.stderr!r}"
    assert "ERROR" in proc.stderr, f"Stderr should contain an ERROR prefix; got: {proc.stderr!r}"


def test_script_rejects_nested_dotenv_stdin() -> None:
    """Feeding `subdir/.env` to the script exits non-zero."""
    proc = _run_hook("subdir/.env\n")
    assert proc.returncode == 1, (
        f"Guard must reject nested `.env`; got returncode={proc.returncode}, stderr={proc.stderr!r}"
    )


def test_script_allows_dotenv_example_stdin() -> None:
    """Feeding `.env.example` to the script exits zero (template is tracked)."""
    proc = _run_hook(".env.example\n")
    assert proc.returncode == 0, (
        f"Guard must allow `.env.example`; got returncode={proc.returncode}, stderr={proc.stderr!r}"
    )


def test_script_allows_empty_stdin() -> None:
    """No staged paths → guard exits zero."""
    proc = _run_hook("")
    assert proc.returncode == 0, (
        f"Guard must allow empty stdin; got returncode={proc.returncode}, stderr={proc.stderr!r}"
    )


def test_script_rejects_force_bypass_stdin() -> None:
    """`git add .env --force` produces the same staged entry as `git add .env`.

    The guard MUST reject it. `--force` is a git-side flag; the pre-commit
    hook only sees what landed in the index, and `.env` is exactly the
    pattern the guard exists to reject.
    """
    proc = _run_hook(".env\n")
    assert proc.returncode == 1, (
        f"Guard must reject `git add .env --force` (i.e., staged `.env`); "
        f"got returncode={proc.returncode}, stderr={proc.stderr!r}"
    )


def test_script_rejects_mixed_list_with_offender() -> None:
    """Mixed list with at least one `.env` MUST be rejected.

    The hook cannot cherry-pick staged paths; if any `.env` is in the
    batch, the commit fails as a whole.
    """
    proc = _run_hook("src/foo.py\n.env\nREADME.md\n")
    assert proc.returncode == 1, (
        f"Guard must reject mixed list containing `.env`; "
        f"got returncode={proc.returncode}, stderr={proc.stderr!r}"
    )


def test_script_allows_unrelated_paths() -> None:
    """Files unrelated to `.env` MUST pass through the guard."""
    proc = _run_hook("src/foo.py\n.env.example\n.env.test\n.env.local\n")
    assert proc.returncode == 0, (
        f"Guard must allow unrelated paths; "
        f"got returncode={proc.returncode}, stderr={proc.stderr!r}"
    )


def test_script_lists_offender_in_stderr() -> None:
    """When the guard rejects, the offending path MUST appear in stderr."""
    proc = _run_hook("src/foo.py\nsecret/.env\nREADME.md\n")
    assert proc.returncode == 1
    assert "secret/.env" in proc.stderr, (
        f"stderr should list the offending path; got: {proc.stderr!r}"
    )


# --- .pre-commit-config.yaml wiring -----------------------------------------


def test_precommit_config_exists() -> None:
    """`.pre-commit-config.yaml` MUST exist at the repo root."""
    assert PRE_COMMIT_CONFIG.exists(), (
        f"Missing pre-commit config: {PRE_COMMIT_CONFIG}. "
        f"Spec scenario 'accidental staging is rejected' requires a "
        f"pre-commit hook that scans staged paths."
    )


def test_precommit_config_registers_no_env_staging_hook() -> None:
    """The config MUST register a hook with id `no-env-staging`."""
    text = PRE_COMMIT_CONFIG.read_text()
    assert "no-env-staging" in text, (
        f"`.pre-commit-config.yaml` must register a hook with id "
        f"`no-env-staging`. Current content:\n{text}"
    )


def test_precommit_config_runs_at_pre_commit_stage() -> None:
    """The hook MUST run at the `pre-commit` stage."""
    text = PRE_COMMIT_CONFIG.read_text()
    assert "pre-commit" in text, f"Hook must run at pre-commit stage. Current content:\n{text}"


def test_precommit_config_is_local_repo() -> None:
    """The hook MUST be defined under `repo: local` (no remote dependency)."""
    text = PRE_COMMIT_CONFIG.read_text()
    assert "repo: local" in text, (
        f"Hook should be defined under `repo: local` (no remote fetch). Current content:\n{text}"
    )
