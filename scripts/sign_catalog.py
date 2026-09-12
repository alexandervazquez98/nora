"""Generate an HMAC-signed OID catalog envelope for an arbitrary triple.

PR 1 (ADR #17): the registry now loads catalogs from two roots (built-in
baseline + operator override). The default invocation still writes the
shipped PMP 450i baseline into ``data/oid-catalogs/cambium/pmp450i/
15.2.1.json`` so the boot path matches pre-PR1 behaviour; passing
``--vendor`` / ``--model`` / ``--firmware`` switches the destination
triple so an operator can re-sign any catalog without touching the
script.

The HMAC key in CI is injected via ``NORA_OID_CATALOG_SIGNING_KEY``;
passing ``--key`` overrides the env var so a developer can sign a
local fixture without exporting secrets.

Public object names only — see ``OID_CATALOG_V1`` below. No MIB prose
is embedded; OIDs are dotted strings sourced from Cambium public
documentation and operator community forums (never from the WHISP-SM-MIB
text, which is EULA-restricted).

Examples:

    # Default: write the shipped PMP 450i baseline into
    # ``data/oid-catalogs/cambium/pmp450i/15.2.1.json``.
    NORA_OID_CATALOG_SIGNING_KEY="change-me" python scripts/sign_catalog.py

    # Sign a different triple into the same repo-root baseline dir.
    NORA_OID_CATALOG_SIGNING_KEY="change-me" \\
        python scripts/sign_catalog.py --vendor cambium --model pmp450i --firmware 15.3.0

    # Sign with an inline key (useful in tests; do NOT do this in CI).
    python scripts/sign_catalog.py --key "local-dev-key"
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Default destination: repo-root ``data/oid-catalogs/`` so the legacy
# invocation stays bit-identical with pre-PR1 (PR 1's built-in baseline
# ships from ``src/nora/data/oid-catalogs/`` and is signed at build time,
# but the helper script keeps writing to the legacy path for operator
# re-sign tooling).
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "data" / "oid-catalogs"

# Public object names -> dotted OID. Each entry has a public reference in
# Cambium's PMP 450i SNMP reference; none of this text is vendored from
# the WHISP-SM-MIB. Use 15 OIDs as the v1 starter set (the spec required
# 15-20 entries; this is the minimum that exercises every fold path).
OID_CATALOG_V1: dict[str, str] = {
    "radioDownlinkRate": "1.3.6.1.4.1.161.19.3.1.1.1.0",
    "radioUplinkRate": "1.3.6.1.4.1.161.19.3.1.1.2.0",
    "signalStrengthRx": "1.3.6.1.4.1.161.19.3.1.1.3.0",
    "signalStrengthTx": "1.3.6.1.4.1.161.19.3.1.1.4.0",
    "ssr": "1.3.6.1.4.1.161.19.3.1.1.5.0",
    "modulationMode": "1.3.6.1.4.1.161.19.3.1.1.6.0",
    "channelBandwidth": "1.3.6.1.4.1.161.19.3.1.1.7.0",
    "frequency": "1.3.6.1.4.1.161.19.3.1.1.8.0",
    "transmitPower": "1.3.6.1.4.1.161.19.3.1.1.9.0",
    "receivePower": "1.3.6.1.4.1.161.19.3.1.1.10.0",
    "jitter": "1.3.6.1.4.1.161.19.3.1.1.11.0",
    "inOctets": "1.3.6.1.4.1.161.19.3.4.1.1.1.0",
    "outOctets": "1.3.6.1.4.1.161.19.3.4.1.1.2.0",
    "linkStatus": "1.3.6.1.4.1.161.19.3.1.1.50.0",
    "upTime": "1.3.6.1.4.1.161.19.3.1.1.51.0",
}


def _resolve_target(
    output_root: Path,
    *,
    vendor: str,
    model: str,
    firmware: str,
) -> Path:
    """Build the catalog path under ``<output_root>/<vendor>/<model>/<firmware>.json``.

    Mirrors the on-disk layout the runtime expects
    (``<root>/<vendor>/<model>/<firmware>.json``); keeping the helper
    path-builder in one place avoids drift when PR 1 introduces the
    built-in baseline under ``src/nora/data/oid-catalogs/``.
    """
    return output_root / vendor / model / f"{firmware}.json"


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sign an OID catalog envelope and write it to disk. Defaults "
            "preserve pre-PR1 behaviour (writes the PMP 450i baseline "
            "to repo-root data/oid-catalogs/)."
        ),
    )
    parser.add_argument(
        "--vendor",
        default="cambium",
        help="Catalog vendor (default: cambium).",
    )
    parser.add_argument(
        "--model",
        default="pmp450i",
        help="Catalog model (default: pmp450i).",
    )
    parser.add_argument(
        "--firmware",
        default="15.2.1",
        help="Catalog firmware string (default: 15.2.1).",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=(
            "Root directory the catalog is written under. Defaults to "
            "<repo>/data/oid-catalogs/ so the legacy invocation stays "
            "bit-identical. PR 1's built-in baseline lives at "
            "<repo>/src/nora/data/oid-catalogs/ and is signed out-of-band."
        ),
    )
    parser.add_argument(
        "--key",
        help=(
            "Inline signing key. Overrides the NORA_OID_CATALOG_SIGNING_KEY "
            "env var. Only use for local development — never commit or "
            "log the resulting envelope."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    key = args.key or os.environ.get("NORA_OID_CATALOG_SIGNING_KEY")
    if not key:
        print(
            "error: NORA_OID_CATALOG_SIGNING_KEY (or --key) is required",
            file=sys.stderr,
        )
        return 2

    target = _resolve_target(
        args.output_root,
        vendor=args.vendor,
        model=args.model,
        firmware=args.firmware,
    )
    target.parent.mkdir(parents=True, exist_ok=True)

    canonical_body = json.dumps(
        OID_CATALOG_V1, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    sig = hmac.new(key.encode("utf-8"), canonical_body, hashlib.sha256).hexdigest()
    envelope = {
        "version": 1,
        "vendor": args.vendor,
        "model": args.model,
        "firmware": args.firmware,
        "oids": OID_CATALOG_V1,
        "hmac_sha256": sig,
    }
    target.write_text(json.dumps(envelope, indent=2))
    print(f"wrote {target} ({len(OID_CATALOG_V1)} OIDs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
