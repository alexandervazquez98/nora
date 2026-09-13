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
# the WHISP-SM-MIB. The v1 starter set is 15 OIDs (the spec required
# 15-20 entries; this is the minimum that exercises every fold path).
# PR 2 (slice 2 of `2026-09-13-pmp450i-production-surface`) extends the
# set with four read-summary OIDs so the ``snmp_get_ap_summary`` and
# ``snmp_get_frame_utilization`` tools can resolve their dotted OIDs
# against the catalog. Per-tool OID membership is recorded in the
# ``TOOLS_V1`` envelope map below; PR 5 indexes it at boot.
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
    # PR 2 — slice 2 read-summary additions.
    "apFirmwareVersion": "1.3.6.1.4.1.161.19.3.1.1.52.0",
    "subscribersCount": "1.3.6.1.4.1.161.19.3.1.1.60.0",
    "frameUtilizationDlPct": "1.3.6.1.4.1.161.19.3.1.1.53.0",
    "frameUtilizationUlPct": "1.3.6.1.4.1.161.19.3.1.1.54.0",
    # PR 3 — slice 3 SM-table additions (sub-cluster 2 — unbiased baseline).
    "smSessionUptime": "1.3.6.1.4.1.161.19.3.2.1.70.0",
    "smCinr": "1.3.6.1.4.1.161.19.3.2.1.71.0",
    "smLinkStatus": "1.3.6.1.4.1.161.19.3.2.1.72.0",
    "smLuid": "1.3.6.1.4.1.161.19.3.2.1.73.0",
    # PR 3 — slice 3 SM diagnostics additions.
    "smJitter": "1.3.6.1.4.1.161.19.3.2.1.80.0",
    "smRetransmits": "1.3.6.1.4.1.161.19.3.2.1.81.0",
    "smRxLevel": "1.3.6.1.4.1.161.19.3.2.1.82.0",
    "smTxLevel": "1.3.6.1.4.1.161.19.3.2.1.83.0",
    # PR 4 — slice 4 spectrum-sweep additions.
    "spectrumNoiseFloorA": "1.3.6.1.4.1.161.19.3.1.1.90.0",
    "spectrumNoiseFloorB": "1.3.6.1.4.1.161.19.3.1.1.91.0",
    "spectrumNoiseFloorC": "1.3.6.1.4.1.161.19.3.1.1.92.0",
    "spectrumChannelRank": "1.3.6.1.4.1.161.19.3.1.1.93.0",
    "spectrumScanStatus": "1.3.6.1.4.1.161.19.3.1.1.94.0",
}


# Per-tool OID-name map — slices 2 + 3 surface (PR 2 + PR 3).
#
# The catalog envelope carries this map so PR 5 can build the
# ``REQUIRED_OIDS_BY_TOOL`` index at boot. The keys are MCP tool
# names registered on the global ``FastMCP("nora")`` instance; the
# values are the OID names (drawn from ``OID_CATALOG_V1``) the tool
# fetches per call. The slice 2 read-summary helpers in
# ``nora.drivers.snmp_pmp450i.summaries`` reuse four legacy names
# (``frequency``, ``channelBandwidth``, ``transmitPower``, ``upTime``)
# from the v1 radio-metrics seed so the read tool has the full
# carrier/channel/tx-power picture without forcing slice 2 to add
# more dotted OIDs. PR 3's ``snmp_get_sm_table`` reads the SM-table
# subtree (one ``walk`` per OID-name base) and folds the response into
# a typed ``SubscriberSummary``; ``snmp_get_sm_detailed_diagnostics``
# reads the four per-SM diagnostics OIDs via individual ``get_oid``
# calls.
TOOLS_V1: dict[str, list[str]] = {
    "snmp_get_ap_summary": [
        "apFirmwareVersion",
        "frequency",
        "channelBandwidth",
        "transmitPower",
        "subscribersCount",
        "upTime",
    ],
    "snmp_get_frame_utilization": [
        "frameUtilizationDlPct",
        "frameUtilizationUlPct",
    ],
    "snmp_get_sm_table": [
        "smSessionUptime",
        "smCinr",
        "smLinkStatus",
        "smLuid",
    ],
    "snmp_get_sm_detailed_diagnostics": [
        "smJitter",
        "smCinr",
        "smRetransmits",
        "smRxLevel",
        "smTxLevel",
    ],
    "snmp_run_spectrum_analysis": [
        "spectrumNoiseFloorA",
        "spectrumNoiseFloorB",
        "spectrumNoiseFloorC",
        "spectrumChannelRank",
        "spectrumScanStatus",
    ],
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
        # PR 2 (slice 2 of `2026-09-13-pmp450i-production-surface`):
        # the envelope carries a per-tool OID-name map so PR 5 can
        # build ``REQUIRED_OIDS_BY_TOOL`` at boot. The HMAC is computed
        # over the canonicalised ``oids`` map only — the tools map is
        # informational and excluded from the signature to keep
        # back-compat with the v1 verification path.
        "tools": TOOLS_V1,
        "hmac_sha256": sig,
    }
    target.write_text(json.dumps(envelope, indent=2))
    print(f"wrote {target} ({len(OID_CATALOG_V1)} OIDs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
