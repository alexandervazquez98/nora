"""Gitignore contract for `data/devices.yaml` (issue #42, Task 8).

Three scenarios pin the contract from the change spec:

* `data/devices.yaml` is gitignored — operators can drop secrets
  under the standard inventory path without accidentally committing
  credentials.
* `data/devices.example.yaml` is NOT gitignored — the public
  template stays tracked.
* The stale "gitignored" claim at `data/devices.example.yaml:5` is
  removed (the claim is currently false; the spec mandates the fix
  in this PR).

Uses ``git check-ignore -v`` via subprocess so the assertions hit
the actual repo's git config rather than a re-implementation of the
gitignore pattern matcher.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_data_devices_yaml_is_gitignored() -> None:
    """`git check-ignore -v data/devices.yaml` exits 0 and names the matching `.gitignore` line."""
    proc = subprocess.run(
        ["git", "check-ignore", "-v", "data/devices.yaml"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 0, (
        f"`git check-ignore data/devices.yaml` must exit 0 (the file is ignored); "
        f"got {proc.returncode}\nstderr={proc.stderr!r}"
    )
    # The output names the matching `.gitignore` line.
    assert proc.stdout, f"`git check-ignore -v` produced no output; stderr={proc.stderr!r}"
    first_line = proc.stdout.splitlines()[0]
    assert ".gitignore" in first_line, (
        f"`git check-ignore -v` output must name a `.gitignore` line; got {first_line!r}"
    )


def test_data_devices_example_yaml_remains_tracked() -> None:
    """`git check-ignore -v data/devices.example.yaml` exits 1 (NOT ignored)."""
    proc = subprocess.run(
        ["git", "check-ignore", "-v", "data/devices.example.yaml"],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert proc.returncode == 1, (
        f"`git check-ignore data/devices.example.yaml` must exit 1 (NOT ignored); "
        f"got {proc.returncode}\nstderr={proc.stderr!r}\nstdout={proc.stdout!r}"
    )


def test_stale_gitignored_claim_removed_from_devices_example_yaml() -> None:
    """Line 5 of `data/devices.example.yaml` no longer claims the file is gitignored."""
    path = PROJECT_ROOT / "data" / "devices.example.yaml"
    lines = path.read_text().splitlines()
    assert len(lines) >= 5, f"devices.example.yaml must have at least 5 lines; got {len(lines)}"
    line_5 = lines[4]
    assert "gitignored" not in line_5, (
        f"line 5 of devices.example.yaml must NOT contain 'gitignored'; got {line_5!r}"
    )
