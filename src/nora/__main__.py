"""`nora` entry point — sub-command dispatcher (issue #43 / WU-2).

Per `openspec/changes/2026-09-15-3tier-tool-governance/specs/nora-mcp-server/spec.md`
R-NEW-7: `nora` argv-dispatches on `argv[1]`.

* `nora mcp`        → existing ``cli.main()`` boot (FastMCP server).
* `nora hitl mint …` → new operator-facing CLI handler that emits a
  signed ``HitlApprovalToken`` JSON payload on stdout.
* `nora` (no args)  → DEPRECATION alias; emits ``DeprecationWarning``
  then calls ``cli.main()`` for back-compat with operators that boot
  NORA via ``python -m nora``.
* unknown sub-command → ``argparse`` style help to stderr + exit 2.

The dispatcher mirrors Unix ``git`` / ``cargo`` conventions and keeps
``nora`` as the only CLI surface (no separate ``nora-hitl`` entry point
— rejected per ADR-3 OPEN QUESTION 3 with rationale "single CLI
surface; mirrors Unix git/cargo; preserves nora no-args back-compat").
"""

from __future__ import annotations

import argparse
import json
import sys
import warnings

# Python's default warning filter ignores `DeprecationWarning` outside
# `__main__`. We relax it so the deprecation notice is visible to
# operators who boot NORA via `python -m nora`.
warnings.simplefilter("always", DeprecationWarning)


def _build_dispatch_parser() -> argparse.ArgumentParser:
    """Build the top-level argparse with `hitl` sub-parser.

    Kept private so a caller can't mutate the dispatcher's state. The
    sub-command names — `mcp`, `hitl` — are part of the operator-facing
    CLI contract; adding a new sub-command is an explicit code change.
    """
    parser = argparse.ArgumentParser(
        prog="nora",
        description=(
            "NORA — Network Operations & Remediation Assistant. "
            "Use `nora mcp` to boot the FastMCP server, "
            "or `nora hitl mint ...` to mint a HITL approval token."
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

    # argparse should have rejected unknown sub-commands, but if we
    # get here defensively exit non-zero.
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
