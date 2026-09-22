"""`nora` entry point — sub-command dispatcher (issue #43 / WU-2 + WU-3).

Per `openspec/changes/2026-09-15-3tier-tool-governance/specs/nora-mcp-server/spec.md`
R-NEW-7: `nora` argv-dispatches on `argv[1]`.

* `nora mcp`              → existing ``cli.main()`` boot (FastMCP server).
* `nora hitl mint …`      → operator-facing CLI handler that emits a
                            signed ``HitlApprovalToken`` JSON payload on stdout.
* `nora prompt sync …`    → issue #45 WU-3 — pushes versioned system prompts
                            to the operator's Open WebUI instance via the
                            declarative REST sync (``POST`` immutable
                            ``<base>-v<X.Y.Z>`` + ``PUT`` mutable
                            ``<base>-latest``).
* `nora` (no args)        → DEPRECATION alias; emits ``DeprecationWarning``
                            then calls ``cli.main()`` for back-compat with
                            operators that boot NORA via ``python -m nora``.
* unknown sub-command → ``argparse`` style help to stderr + exit 2.

The dispatcher mirrors Unix ``git`` / ``cargo`` conventions and keeps
``nora`` as the only CLI surface (no separate ``nora-hitl`` entry point
— rejected per ADR-3 OPEN QUESTION 3 with rationale "single CLI
surface; mirrors Unix git/cargo; preserves nora no-args back-compat").
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path

# Python's default warning filter ignores `DeprecationWarning` outside
# `__main__`. We relax it so the deprecation notice is visible to
# operators who boot NORA via `python -m nora`.
warnings.simplefilter("always", DeprecationWarning)


def _build_dispatch_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse with `hitl` + `prompt` sub-parsers.

    Kept private so a caller can't mutate the dispatcher's state. The
    sub-command names — `mcp`, `hitl`, `prompt` — are part of the
    operator-facing CLI contract; adding a new sub-command is an
    explicit code change.
    """
    parser = argparse.ArgumentParser(
        prog="nora",
        description=(
            "NORA — Network Operations & Remediation Assistant. "
            "Use `nora mcp` to boot the FastMCP server, "
            "`nora hitl mint ...` to mint a HITL approval token, "
            "`nora prompt sync ...` to push versioned system prompts "
            "to an Open WebUI instance, "
            "`nora upgrade ...` to safely upgrade an existing NORA "
            "install with automatic rollback, "
            "or `nora doctor ...` to render the install health summary "
            "(re-uses `scripts/verify-install.sh --json` plus a sysctl "
            "persistence cross-check)."
        ),
    )
    subparsers = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")

    # `nora mcp` — boot MCP. The remaining CLI flags (--transport, etc.)
    # are handled by `cli.main()` once we dispatch here.
    subparsers.add_parser(
        "mcp",
        help="Boot the NORA FastMCP server (default transport: stdio).",
    )

    # `nora hitl mint` — operator-facing token mint.
    hitl_parser = subparsers.add_parser(
        "hitl",
        help="HITL approval-token operations.",
    )
    hitl_subparsers = hitl_parser.add_subparsers(dest="hitl_command", metavar="HITL_COMMAND")
    mint_parser = hitl_subparsers.add_parser(
        "mint",
        help="Mint a signed HITL approval token.",
    )
    mint_parser.add_argument(
        "--operator-id",
        required=True,
        help="Operator id bound to the token (required).",
    )
    mint_parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=None,
        help=(
            "Token time-to-live in seconds (default: "
            "Settings.nora_hitl_token_ttl_seconds, typically 900)."
        ),
    )

    # `nora prompt sync` — issue #45 WU-3 — declarative Open WebUI sync.
    # The dispatcher calls the sync module's ``sync_prompts`` function
    # in-process (no subprocess) so the operator gets typed exceptions
    # and JSON output without paying a Python startup per sync.
    prompt_parser = subparsers.add_parser(
        "prompt",
        help="Prompt-registry operations (sync versioned system prompts to chat front-ends).",
    )
    prompt_subparsers = prompt_parser.add_subparsers(
        dest="prompt_command", metavar="PROMPT_COMMAND"
    )
    sync_parser = prompt_subparsers.add_parser(
        "sync",
        help=(
            "Sync versioned system prompts to Open WebUI. "
            "Pushes the immutable `<base>-v<X.Y.Z>` profile and updates "
            "the mutable `<base>-latest` alias in place."
        ),
    )
    sync_parser.add_argument(
        "--base-url",
        default="http://localhost:8080",
        help="Open WebUI base URL (default: http://localhost:8080; env: OPENWEBUI_BASE_URL).",
    )
    sync_parser.add_argument(
        "--admin-api-key",
        default=None,
        help=(
            "Open WebUI admin API key (env: OPENWEBUI_ADMIN_API_KEY). "
            "CLI flag wins over env; one of the two MUST be set."
        ),
    )
    sync_parser.add_argument(
        "--model-base",
        default="nora-netops",
        help="Model handle prefix (default: nora-netops).",
    )
    sync_parser.add_argument(
        "--nora-version",
        default=None,
        help="NORA version stamped into metadata (default: nora.__version__).",
    )
    sync_parser.add_argument(
        "--git-sha",
        default="unknown",
        help="Git commit SHA stamped into metadata (default: 'unknown').",
    )
    sync_parser.add_argument(
        "--release-tag",
        default=None,
        help="Optional release tag (e.g. v0.3.5) stamped into metadata.",
    )

    # `nora upgrade` — issue #60 / PR-2 — automate the update
    # procedure documented in OPERATIONS.md. Headless-only (D9);
    # mirrors the `nora prompt sync` arg pattern (D8): sub-command +
    # argparse + explicit exit codes (0/1/2).
    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help=(
            "Upgrade an existing NORA install with pre-flight backup "
            "and automatic rollback on phase failure."
        ),
    )
    upgrade_parser.add_argument(
        "--ref",
        default="origin/main",
        help=("Target git ref (tag, branch, or full commit SHA). Default: origin/main."),
    )
    upgrade_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show every phase without mutating the filesystem.",
    )
    upgrade_parser.add_argument(
        "--check-only",
        action="store_true",
        help="Pre-flight check only; do not upgrade.",
    )
    upgrade_parser.add_argument(
        "--no-backup",
        action="store_true",
        help="Skip the pre-upgrade backup (operator manages snapshots externally).",
    )
    upgrade_parser.add_argument(
        "--no-restart",
        action="store_true",
        help="Don't restart nora-mcp after upgrade (useful for offline validation).",
    )
    upgrade_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout.",
    )
    upgrade_parser.add_argument(
        "--prefix",
        default="/opt/nora",
        help="Install prefix (matches install.sh; default: /opt/nora).",
    )
    upgrade_parser.add_argument(
        "--config-dir",
        default="/etc/nora",
        help="Runtime config dir (matches install.sh; default: /etc/nora).",
    )
    upgrade_parser.add_argument(
        "--state-dir",
        default="/var/lib/nora",
        help="Mutable state dir (matches install.sh; default: /var/lib/nora).",
    )
    upgrade_parser.add_argument(
        "--log-dir",
        default="/var/log/nora",
        help="Log dir (matches install.sh; default: /var/log/nora).",
    )
    upgrade_parser.add_argument(
        "--user",
        default="nora",
        help="Service-account username (matches install.sh; default: nora).",
    )

    # `nora doctor` — issue #60 / PR-3 — render the install health
    # check. Mirrors `nora prompt sync` + `nora upgrade` arg pattern
    # (D4 / D8): headless-only (D9), explicit exit codes (0/1/2),
    # thin renderer over `scripts/verify-install.sh --json` + a
    # `net.ipv4.ping_group_range` persistence cross-check (D10).
    doctor_parser = subparsers.add_parser(
        "doctor",
        help=(
            "Render the install health summary as a human-readable "
            "table (or JSON with `--json`). Reuses "
            "`scripts/verify-install.sh --json` and adds a sysctl "
            "persistence cross-check."
        ),
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON to stdout.",
    )
    doctor_parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 on any WARN (in addition to the default exit 1 on FAIL).",
    )
    doctor_parser.add_argument(
        "--no-systemd",
        action="store_true",
        help="Pass `--no-systemd` to `verify-install.sh` (skip the systemd listener check).",
    )
    doctor_parser.add_argument(
        "--http",
        action="store_true",
        help="Pass `--check-http` to `verify-install.sh` (probe the MCP HTTP listener).",
    )
    doctor_parser.add_argument(
        "--prefix",
        default="/opt/nora",
        help="Install prefix (matches install.sh; default: /opt/nora).",
    )
    doctor_parser.add_argument(
        "--config-dir",
        default="/etc/nora",
        help="Runtime config dir (matches install.sh; default: /etc/nora).",
    )
    doctor_parser.add_argument(
        "--state-dir",
        default="/var/lib/nora",
        help="Mutable state dir (matches install.sh; default: /var/lib/nora).",
    )
    doctor_parser.add_argument(
        "--log-dir",
        default="/var/log/nora",
        help="Log dir (matches install.sh; default: /var/log/nora).",
    )
    doctor_parser.add_argument(
        "--user",
        default="nora",
        help="Service-account username (matches install.sh; default: nora).",
    )

    return parser


def _dispatch_hitl_mint(args: argparse.Namespace) -> int:
    """Handle `nora hitl mint` — emit a signed JSON token on stdout.

    Returns the exit code (0 success, non-zero on missing/invalid
    signing key). argparse validates the required flags before this
    runs, so the only failure modes are runtime (signing key empty).
    """
    # Imported lazily so the dispatcher's import cost is paid only when
    # `nora hitl mint` is invoked.
    from nora.config import Settings
    from nora.hitl.tokens import AutonomousMutationRejected, mint_token

    settings = Settings()
    signing_key = settings.nora_hitl_signing_key
    if signing_key is None:
        sys.stderr.write(
            "nora: NORA_HITL_SIGNING_KEY is empty; cannot mint signed tokens. "
            "Set the env var to a non-empty value and re-run.\n"
        )
        return 2

    ttl_seconds = args.ttl_seconds
    if ttl_seconds is None:
        ttl_seconds = settings.nora_hitl_token_ttl_seconds

    try:
        token = mint_token(
            args.operator_id,
            ttl_seconds=ttl_seconds,
            signing_key=signing_key,
        )
    except AutonomousMutationRejected as exc:
        sys.stderr.write(f"nora: {exc}\n")
        return 2

    payload = token.model_dump(mode="json")
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()
    return 0


def _dispatch_prompt_sync(args: argparse.Namespace) -> int:
    """Handle `nora prompt sync` — push versioned prompts to Open WebUI.

    Resolves ``admin_api_key`` from CLI > env (``OPENWEBUI_ADMIN_API_KEY``);
    one of the two MUST be set or the dispatcher exits 2 with a stderr
    message naming the missing key. The registry is built from the
    default ``Settings`` (so operator-overridden ``NORA_PROMPTS_DIR``
    applies), then ``sync_prompts`` is called in-process — no
    subprocess, no boot of the FastMCP server.

    Returns the exit code (0 success, 1 sync error, 2 missing key).
    On success, stdout carries a JSON array of result dicts (one per
    synced prompt). On any ``OpenWebUISyncError`` /
    ``OpenWebUIAuthError`` the message is prefixed with ``nora:
    `` on stderr so the operator can grep the audit log.
    """
    # Lazy imports — keep the dispatcher's import cost paid only when
    # `nora prompt sync` is invoked.
    from nora import __version__ as nora_version
    from nora.config import Settings
    from nora.prompts.registry import PromptRegistry
    from nora.prompts.sync import (
        OpenWebUIAuthError,
        OpenWebUIConfig,
        OpenWebUISyncError,
        sync_prompts,
    )

    # Resolve admin API key: CLI flag > env var. Fail-closed.
    admin_api_key = args.admin_api_key or os.environ.get("OPENWEBUI_ADMIN_API_KEY")
    if not admin_api_key:
        sys.stderr.write(
            "nora: missing OPENWEBUI_ADMIN_API_KEY env var or --admin-api-key flag; "
            "refusing to sync against an unauthenticated Open WebUI surface.\n"
        )
        return 2

    # CLI flag > env var on base_url too (parenthesise the precedence clearly).
    base_url = args.base_url or os.environ.get("OPENWEBUI_BASE_URL", "http://localhost:8080")

    # `nora_version`: explicit `--nora-version` flag wins; fall back
    # to the package metadata otherwise. Operators running from a
    # tarball install see `nora.__version__` automatically.
    resolved_nora_version = args.nora_version or nora_version

    config = OpenWebUIConfig(
        base_url=base_url,
        admin_api_key=admin_api_key,
        model_base=args.model_base,
    )

    # Real registry from default Settings — same surface as `nora mcp`
    # boot, so operators see consistent prompt discovery.
    registry = PromptRegistry.from_settings(Settings())

    try:
        results = sync_prompts(
            registry,
            config,
            nora_version=resolved_nora_version,
            git_sha=args.git_sha,
            release_tag=args.release_tag,
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


def _serialise_for_json(obj: object) -> object:
    """Recursively convert dataclass instances + Paths into JSON-safe scalars.

    Walks dataclasses, lists, tuples, dicts, and ``Path`` so the
    ``--json`` output is stable across operator upgrades without
    dragging in a third-party serialiser. Returns the input as-is for
    scalar types (``str``, ``int``, ``float``, ``bool``, ``None``).
    """
    from dataclasses import asdict, is_dataclass

    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialise_for_json(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): _serialise_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialise_for_json(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _dispatch_upgrade(args: argparse.Namespace) -> int:
    """Handle `nora upgrade` — automate the OPERATIONS.md update procedure.

    Behaviour (mirrors the `nora prompt sync` dispatcher pattern):

    1. Pre-flight via ``nora.upgrade.preflight``. If ``result.noop``,
       print a one-line stderr message and exit 0 (no upgrade needed).
    2. ``--check-only`` → print the preflight result + exit 0 without
       touching the filesystem.
    3. ``--no-backup`` skips ``create_backup``; otherwise create the
       backup and print ``backup: <path>`` to stderr.
    4. Run the upgrade phases via ``run_upgrade``. On ``UpgradeFailed``
       print the failing phase + backup path to stderr and exit 1.
    5. ``--json`` emits ``json.dumps(asdict(result))`` (with nested
       dataclasses + Paths serialised recursively) on stdout.
    6. Otherwise print a human-readable summary on stderr.
    7. Exit 0 on success.

    Returns the exit code. argparse validates the required flags
    before this runs; the only failure modes are runtime (preflight,
    backup, upgrade, smoke).
    """
    # Lazy imports — keep the dispatcher's import cost paid only when
    # `nora upgrade` is invoked.
    from nora import upgrade as upgrade_mod

    try:
        preflight_result = upgrade_mod.preflight(
            prefix=Path(args.prefix),
            ref=args.ref,
        )
    except upgrade_mod.PreFlightError as exc:
        sys.stderr.write(f"nora: preflight failed: {exc}\n")
        return 1

    if preflight_result.noop:
        sys.stderr.write(f"already at {preflight_result.target_sha}, nothing to do\n")
        return 0

    if args.check_only:
        sys.stderr.write(
            f"check-only: ref={args.ref} "
            f"current_sha={preflight_result.current_sha} "
            f"target_sha={preflight_result.target_sha}\n"
        )
        if args.json:
            sys.stdout.write(json.dumps(_serialise_for_json(preflight_result)) + "\n")
            sys.stdout.flush()
        return 0

    backup_path: Path | None = None
    if not args.no_backup:
        timestamp = upgrade_mod._utc_timestamp()  # noqa: SLF001 — internal helper
        backup_path = upgrade_mod.create_backup(
            prefix=Path(args.prefix),
            config_dir=Path(args.config_dir),
            state_dir=Path(args.state_dir),
            timestamp=timestamp,
            dry_run=args.dry_run,
        )
        sys.stderr.write(f"backup: {backup_path}\n")

    try:
        result = upgrade_mod.run_upgrade(
            prefix=Path(args.prefix),
            target_sha=preflight_result.target_sha or "",
            backup_path=backup_path,
            config_dir=Path(args.config_dir),
            state_dir=Path(args.state_dir),
            log_dir=Path(args.log_dir),
            user=args.user,
            dry_run=args.dry_run,
            restart=not args.no_restart,
        )
    except upgrade_mod.UpgradeFailed as exc:
        backup_note = f" backup={exc.backup_path}" if exc.backup_path else ""
        sys.stderr.write(f"nora: upgrade failed at phase={exc.phase}{backup_note}: {exc}\n")
        return 1
    except upgrade_mod.BackupError as exc:
        sys.stderr.write(f"nora: backup failed: {exc}\n")
        return 1
    except upgrade_mod.PreFlightError as exc:
        sys.stderr.write(f"nora: preflight failed: {exc}\n")
        return 1

    if args.json:
        sys.stdout.write(json.dumps(_serialise_for_json(result)) + "\n")
        sys.stdout.flush()
        return 0

    # Human-readable summary on stderr (so stdout stays clean for
    # piping). Smoke summary counts are surfaced for at-a-glance
    # operator review; the full report goes through `--json`.
    smoke_report = result.smoke_report or {}
    summary = smoke_report.get("summary") if isinstance(smoke_report, dict) else None
    if isinstance(summary, dict):
        ok = summary.get("ok", "?")
        warn = summary.get("warn", "?")
        fail = summary.get("fail", "?")
        smoke_line = f"smoke=ok:{ok} warn:{warn} fail:{fail}"
    else:
        smoke_line = "smoke=(not run — dry-run)" if args.dry_run else "smoke=(no report)"

    sys.stderr.write(
        f"upgrade complete: prefix={args.prefix} "
        f"current_sha={preflight_result.current_sha} "
        f"target_sha={result.final_sha} "
        f"phases={','.join(result.phases_completed)} "
        f"{smoke_line}\n"
    )
    return 0


def _doctor_exit_code(*, fail_count: int, warn_count: int, strict: bool) -> int:
    """Map a doctor report's counts to the dispatcher's exit code.

    Mirrors the exit-code contract in ``scripts/verify-install.sh``:

    * ``0`` — every check is ``ok`` (WARN alone does NOT trip exit 1
      unless ``--strict`` was passed).
    * ``1`` — at least one FAIL.
    * ``2`` — ``--strict`` AND at least one WARN (no FAIL).

    Kept as a free function so the dispatcher's exit-code logic is
    unit-testable without spinning up the whole dispatcher.
    """
    if fail_count > 0:
        return 1
    if strict and warn_count > 0:
        return 2
    return 0


def _dispatch_doctor(args: argparse.Namespace) -> int:
    """Handle ``nora doctor`` — render the install health summary.

    Behaviour (mirrors the ``nora prompt sync`` dispatcher pattern):

    1. Call ``doctor.run_doctor(...)`` with the kwargs from
       ``args``. Catch ``doctor.VerifyInstallFailed`` and emit a
       clear stderr line; return 1 (the dispatcher still prints the
       partial report that ``run_doctor`` built so the operator
       sees SOMETHING).
    2. If ``args.json``: print ``doctor.render_json(report)`` to
       stdout. Otherwise: print ``doctor.render_human(report)`` to
       stderr (stdout stays clean for piping).
    3. Exit code: 0 on a clean report, 1 on any FAIL, 2 on
       ``--strict`` with any WARN.
    """
    # Lazy imports — keep the dispatcher's import cost paid only
    # when `nora doctor` is invoked.
    from nora import doctor as doctor_mod

    try:
        report = doctor_mod.run_doctor(
            prefix=Path(args.prefix),
            config_dir=Path(args.config_dir),
            state_dir=Path(args.state_dir),
            log_dir=Path(args.log_dir),
            user=args.user,
            skip_systemd=args.no_systemd,
            check_http=args.http,
        )
    except doctor_mod.VerifyInstallFailed as exc:
        sys.stderr.write(f"nora: doctor failed: {exc}\n")
        if exc.stderr:
            sys.stderr.write(f"nora: verify-install.sh stderr:\n{exc.stderr}\n")
        return 1

    # The human-render ALWAYS goes to stderr so operators running
    # interactively (with or without `--json`) see a readable
    # summary. CI scripts ignore stderr and consume stdout JSON.
    sys.stderr.write(doctor_mod.render_human(report))
    sys.stderr.flush()

    if args.json:
        sys.stdout.write(doctor_mod.render_json(report) + "\n")
        sys.stdout.flush()

    return _doctor_exit_code(
        fail_count=report.fail_count,
        warn_count=report.warn_count,
        strict=args.strict,
    )


def _deprecation_alias_boots_mcp() -> int:
    """`nora` (no args) — emit deprecation warning then delegate to cli.main.

    The legacy alias path; preserved for one minor release so existing
    operators / dashboards that boot NORA via `python -m nora` keep
    working. The canonical entry point is `nora-mcp` → `nora.cli.main`.
    """
    warnings.warn(
        "`python -m nora` (or `nora` with no args) is deprecated and "
        "will be removed in the next minor release. "
        "Use the `nora-mcp` console script or `nora mcp` instead.",
        DeprecationWarning,
        stacklevel=2,
    )
    from nora.cli import main as cli_main

    cli_main()
    return 0


def main(argv: list[str] | None = None) -> int:
    """Top-level `nora` dispatcher.

    Back-compat: an empty argv (or argv == None → empty list) routes
    through the deprecation alias. `nora mcp` boots MCP. `nora hitl
    mint …` mints a signed token. Unknown sub-commands exit 2 with
    argparse help on stderr.

    The function returns an int exit code; the top-level `if __name__`
    guard forwards it to `sys.exit`.
    """
    if argv is None:
        argv = sys.argv[1:]

    # Back-compat: no sub-command → deprecation alias (boots MCP).
    if not argv:
        return _deprecation_alias_boots_mcp()

    parser = _build_dispatch_parser()
    args = parser.parse_args(argv)

    # `nora mcp` → boot MCP via cli.main.
    if args.subcommand == "mcp":
        from nora.cli import main as cli_main

        cli_main()
        return 0

    # `nora hitl [mint]` → operator-facing mint.
    if args.subcommand == "hitl":
        if args.hitl_command != "mint":
            # `nora hitl` with no sub-command — print hitl help.
            sys.stderr.write("nora hitl: missing sub-command (expected `mint`)\n")
            sys.exit(2)
        return _dispatch_hitl_mint(args)

    # `nora prompt [sync]` → issue #45 WU-3 Open WebUI sync.
    if args.subcommand == "prompt":
        if args.prompt_command != "sync":
            # `nora prompt` with no sub-command — print prompt help.
            sys.stderr.write("nora prompt: missing sub-command (expected `sync`)\n")
            sys.exit(2)
        return _dispatch_prompt_sync(args)

    # `nora upgrade` → issue #60 / PR-2 — automate the OPERATIONS.md
    # update procedure with pre-flight backup + automatic rollback.
    if args.subcommand == "upgrade":
        return _dispatch_upgrade(args)

    # `nora doctor` → issue #60 / PR-3 — render the install health
    # summary (re-uses `scripts/verify-install.sh --json` + a sysctl
    # persistence cross-check).
    if args.subcommand == "doctor":
        return _dispatch_doctor(args)

    # argparse should have rejected unknown sub-commands, but if we
    # get here defensively exit non-zero.
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
