"""Atomic JSON write + 0o600 mode + dir validation (R3, R4, R18).

The session journal's durability primitive lives in `session_paths.py`.
Every later commit depends on `atomic_write_json` returning only after
`os.replace` succeeded, so the file is either the old content (if the
write was interrupted before replace) or the new content (after replace)
— never a torn mix.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

# Mark the entire file as POSIX-only for the chmod-0o700 / 0o600 assertions;
# non-POSIX paths are skipped per R18 platform guard.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="0o600 mode is POSIX-only; non-POSIX is best-effort",
)


# ---------------------------------------------------------------------------
# atomic_write_json — temp-file + os.replace, then chmod 0o600
# ---------------------------------------------------------------------------


def test_atomic_write_creates_temp_then_renames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`atomic_write_json` MUST write to `<file>.tmp` first, then rename.

    We monkeypatch `os.replace` to record the arguments and assert the
    rename targets the canonical path (not the .tmp).
    """
    from nora.core.session_paths import atomic_write_json

    target = tmp_path / "session.json"
    seen: list[tuple[str, str]] = []

    real_replace = os.replace

    def spy_replace(src: str, dst: str) -> None:
        seen.append((os.fspath(src), os.fspath(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    atomic_write_json(target, {"hello": "world"})

    # `seen` records exactly one replace; src is a .tmp suffix, dst is the
    # canonical path.
    assert len(seen) == 1, f"Expected exactly one os.replace; got {seen!r}"
    src, dst = seen[0]
    assert src.endswith(".tmp"), f"rename source must end in .tmp; got {src!r}"
    assert Path(dst).resolve() == target.resolve(), (
        f"rename dst must match the target; got {dst!r} vs {target!r}"
    )


def test_atomic_write_target_parses_as_valid_json(tmp_path: Path) -> None:
    """After `atomic_write_json`, the canonical file parses as JSON with the payload."""
    from nora.core.session_paths import atomic_write_json

    target = tmp_path / "session.json"
    payload = {"session_id": "abc", "trace": []}
    atomic_write_json(target, payload)

    with target.open() as f:
        loaded = json.load(f)
    assert loaded == payload


@_POSIX_ONLY
def test_atomic_write_sets_posix_0o600_mode(tmp_path: Path) -> None:
    """On POSIX, the canonical file's mode MUST be 0o600 (R18)."""
    from nora.core.session_paths import atomic_write_json

    target = tmp_path / "session.json"
    atomic_write_json(target, {"k": "v"})

    mode = target.stat().st_mode & 0o777
    assert mode == 0o600, f"Expected mode 0o600; got 0o{mode:o}"


def test_atomic_write_survives_interrupted_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If `os.replace` is interrupted, the canonical file still parses (R4).

    Simulated: replace raises BEFORE swapping. The previous canonical file
    (if any) MUST remain valid; the orphan .tmp is allowed to remain.
    """
    from nora.core.session_paths import atomic_write_json

    target = tmp_path / "session.json"
    canonical_old = {"phase": "V1"}
    atomic_write_json(target, canonical_old)

    def exploding_replace(src: str, dst: str) -> None:
        raise OSError("simulated crash mid-rename")

    monkeypatch.setattr(os, "replace", exploding_replace)

    with pytest.raises(OSError):
        atomic_write_json(target, {"phase": "V2"})

    # The canonical file still parses with the old content.
    with target.open() as f:
        assert json.load(f) == canonical_old
    # An orphan .tmp may exist; it MUST NOT corrupt the canonical read.
    tmp_candidates = list(tmp_path.glob("*.tmp"))
    assert tmp_candidates, "Expected an orphan .tmp after interrupted replace"


# ---------------------------------------------------------------------------
# ensure_journal_dir — mkdir 0o700 + refuse symlink escape
# ---------------------------------------------------------------------------


@_POSIX_ONLY
def test_ensure_journal_dir_creates_dir_at_0o700(tmp_path: Path) -> None:
    """`ensure_journal_dir` MUST mkdir with mode 0o700 (POSIX)."""
    from nora.core.session_paths import ensure_journal_dir

    target = tmp_path / "sessions"
    resolved = ensure_journal_dir(target)

    assert resolved.is_dir(), f"{resolved} is not a directory"
    mode = resolved.stat().st_mode & 0o777
    assert mode == 0o700, f"Expected mode 0o700; got 0o{mode:o}"


def test_ensure_journal_dir_is_idempotent(tmp_path: Path) -> None:
    """A second `ensure_journal_dir` on the same path MUST NOT error."""
    from nora.core.session_paths import ensure_journal_dir

    target = tmp_path / "sessions"
    ensure_journal_dir(target)
    # No exception on second call.
    ensure_journal_dir(target)
    assert target.is_dir()


@_POSIX_ONLY
def test_ensure_journal_dir_refuses_symlink_escape(tmp_path: Path) -> None:
    """A symlink pointing OUTSIDE the parent of the journal dir MUST be rejected.

    Scenario: an attacker pre-creates `sessions -> /var/log/system` so writes
    to `sessions` actually land in `/var/log/system`. The dir path
    `tmp_path/sessions` is itself a symlink whose resolution is OUTSIDE
    `tmp_path` (its parent). `ensure_journal_dir` MUST refuse.
    """
    from nora.core.session_paths import ensure_journal_dir

    # Create the real target directory OUTSIDE `tmp_path` (in the real filesystem).
    outside_target = Path("/tmp/nora-symlink-escape-test-target")
    outside_target.mkdir(parents=True, exist_ok=True)
    try:
        link = tmp_path / "sessions"
        link.symlink_to(outside_target)

        with pytest.raises(OSError) as excinfo:
            ensure_journal_dir(link)

        assert "symlink" in str(excinfo.value).lower() or "escape" in str(excinfo.value).lower()
    finally:
        # Clean up the real target so the test is hermetic.
        import shutil

        if outside_target.exists():
            shutil.rmtree(outside_target, ignore_errors=True)


# ---------------------------------------------------------------------------
# canonical_path / ndjson_path — resolve under journal_dir
# ---------------------------------------------------------------------------


def test_canonical_path_is_under_journal_dir(tmp_path: Path) -> None:
    """`canonical_path(journal_dir, session_id)` MUST return a Path inside journal_dir."""
    from nora.core.session_paths import canonical_path

    p = canonical_path(tmp_path, "abc-123")
    assert p.parent.resolve() == tmp_path.resolve(), (
        f"canonical_path landed outside journal_dir: {p!r}"
    )
    assert p.name == "abc-123.json"


def test_ndjson_path_is_under_journal_dir(tmp_path: Path) -> None:
    """`ndjson_path(journal_dir, session_id)` MUST return a Path inside journal_dir."""
    from nora.core.session_paths import ndjson_path

    p = ndjson_path(tmp_path, "abc-123")
    assert p.parent.resolve() == tmp_path.resolve(), (
        f"ndjson_path landed outside journal_dir: {p!r}"
    )
    assert p.name == "abc-123.log.ndjson"
