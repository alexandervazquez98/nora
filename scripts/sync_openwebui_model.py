"""Thin CLI wrapper for the Open WebUI sync layer — issue #45 / WU-3.

Operators invoke this when they want to run sync outside the
``nora prompt sync`` dispatcher (e.g. from a deploy hook with a
custom path resolution, or a tarball install where the venv's
console script ``nora`` is not on PATH).

Resolution order (highest wins):

1. CLI flags (``--base-url``, ``--admin-api-key``, ...).
2. Process env vars (``OPENWEBUI_BASE_URL``, ``OPENWEBUI_ADMIN_API_KEY``).
3. Optional ``.env`` at ``PROJECT_ROOT/.env`` (stdlib parser — no
   ``python-dotenv``; we keep the dep surface tight).

Exit codes:

* ``0`` — sync ran; JSON array on stdout.
* ``1`` — ``OpenWebUISyncError`` / ``OpenWebUIAuthError`` (transport).
* ``2`` — missing ``OPENWEBUI_ADMIN_API_KEY`` / ``--admin-api-key``.

Zero-Leakage: prints NO prompt names on stderr (operator logs only);
the JSON array on stdout contains the prompt names from the
registry — same surface as ``nora prompt sync``, so the
``scripts/`` and the dispatcher share their audit output shape.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

# Project root is the parent of ``scripts/``. Used to discover the
# sibling ``.env`` file when run from a tarball install.
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


def _load_dotenv_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict using only the stdlib.

    Tiny format: ``KEY=VALUE`` per line, ``#`` starts a comment,
    optional surrounding quotes stripped. No shell expansion —
    ``$VAR`` is treated as a literal value (operators can resolve
    their secrets via ``env -i`` + ``envsubst`` if they need it).
    Lines without ``=`` are silently ignored.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip a single pair of surrounding quotes; intentionally
        # do NOT escape inner quotes — operators control the file.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        out[key] = value
    return out


def _resolve_git_sha(repo_root: Path) -> str:
    """Best-effort ``git rev-parse HEAD``; fall back to ``"unknown"``.

    Tarball installs (no ``.git``) return ``"unknown"`` silently —
    the prompt registry still works, the metadata just carries a
    non-empty placeholder so Open WebUI's audit dropdown has
    something stable to display.
    """
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"
    if completed.returncode != 0:
        return "unknown"
    sha = completed.stdout.strip()
    return sha if sha else "unknown"


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.sync_openwebui_model",
        description=(
            "Push versioned NORA system prompts to an Open WebUI instance "
            "via the declarative REST sync layer. Equivalent to "
            "`nora prompt sync`; use this when the operator console "
            "script is not on PATH."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="Open WebUI base URL (env: OPENWEBUI_BASE_URL; default: http://localhost:8080).",
    )
    parser.add_argument(
        "--admin-api-key",
        default=None,
        help=(
            "Open WebUI admin API key (env: OPENWEBUI_ADMIN_API_KEY). "
            "CLI flag wins; one of the two MUST be set."
        ),
    )
    parser.add_argument(
        "--model-base",
        default=None,
        help="Model handle prefix (default: nora-netops; env: OPENWEBUI_MODEL_BASE).",
    )
    parser.add_argument(
        "--nora-version",
        default=None,
        help="NORA version stamped into metadata (default: nora.__version__).",
    )
    parser.add_argument(
        "--git-sha",
        default=None,
        help=(
            "Git commit SHA stamped into metadata. Default: "
            "`git rev-parse HEAD` resolved at the repo root; "
            "fallback 'unknown' for tarball installs."
        ),
    )
    parser.add_argument(
        "--release-tag",
        default=None,
        help="Optional release tag (e.g. v0.3.5) stamped into metadata.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Resolve config + env, build the registry, run sync, print JSON.

    Mirrors ``nora.__main__._dispatch_prompt_sync`` so the two entry
    points stay behaviourally identical. Returns the exit code:
    0 (success), 1 (transport error), 2 (missing auth key).
    """
    # Imports deferred so the script module is importable without the
    # ``nora`` package on PATH (e.g. when packaging a wheel-distributable
    # CLI shim).
    from nora import __version__ as nora_version
    from nora.config import Settings
    from nora.prompts.registry import PromptRegistry
    from nora.prompts.sync import (
        OpenWebUIAuthError,
        OpenWebUIConfig,
        OpenWebUISyncError,
        sync_prompts,
    )

    args = _build_arg_parser().parse_args(argv)

    # Merge: .env < process env < CLI args (each layer overrides the
    # previous). The merge is done by building a single layered view;
    # explicit None vs empty-string is preserved so an explicit
    # `--base-url ""` would still error (Pydantic rejects blank URLs).
    dotenv = _load_dotenv_file(PROJECT_ROOT / ".env")

    def _resolve(cli_value: str | None, env_key: str, default: str | None = None) -> str | None:
        if cli_value is not None:
            return cli_value
        if env_key in os.environ:
            return os.environ[env_key]
        if env_key in dotenv:
            return dotenv[env_key]
        return default

    base_url = _resolve(args.base_url, "OPENWEBUI_BASE_URL", "http://localhost:8080")
    admin_api_key = _resolve(args.admin_api_key, "OPENWEBUI_ADMIN_API_KEY", None)
    model_base = _resolve(args.model_base, "OPENWEBUI_MODEL_BASE", "nora-netops")
    release_tag = args.release_tag
    nora_ver = args.nora_version or nora_version
    git_sha = args.git_sha or _resolve_git_sha(PROJECT_ROOT) or "unknown"

    if not admin_api_key:
        sys.stderr.write(
            "nora: missing OPENWEBUI_ADMIN_API_KEY env var or --admin-api-key flag; "
            "refusing to sync against an unauthenticated Open WebUI surface.\n"
        )
        return 2

    if not base_url:
        sys.stderr.write("nora: missing OPENWEBUI_BASE_URL (CLI or env)\n")
        return 2

    config = OpenWebUIConfig(
        base_url=base_url,
        admin_api_key=admin_api_key,
        model_base=model_base or "nora-netops",
    )

    # Real registry — same surface as `nora prompt sync`.
    registry = PromptRegistry.from_settings(Settings())

    try:
        results: list[dict[str, Any]] = sync_prompts(
            registry,
            config,
            nora_version=nora_ver,
            git_sha=git_sha,
            release_tag=release_tag,
        )
    except OpenWebUIAuthError as exc:
        sys.stderr.write(f"nora: Open WebUI auth failure: {exc}\n")
        return 1
    except OpenWebUISyncError as exc:
        sys.stderr.write(f"nora: Open WebUI sync failure: {exc}\n")
        return 1

    sys.stdout.write(json.dumps(results) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
