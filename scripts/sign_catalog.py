"""Generate an HMAC-signed OID catalog envelope from a source JSON file.

PR 1 (ADR #17): the registry loads catalogs from two roots (built-in
baseline + operator override). The default invocation reads
``data/oid-catalogs/sources/cambium/pmp450i/15.2.1.source.json`` (the
editable source-of-truth) and writes the signed envelope into
``data/oid-catalogs/cambium/pmp450i/15.2.1.json`` so the boot path
matches pre-refactor behaviour. ``--all`` signs every ``*.source.json``
file under the source root in one pass — the recommended workflow for
new firmware drops (no Python change required).

PR 35: the source JSON separates *data* (verified canonical OID values)
from *schema* (per-tool OID membership, the ``TOOLS_V1`` map below stays
in code as a contract between the catalog and the @mcp.tool bodies).
Adding a new firmware = drop a new ``<firmware>.source.json`` and run
``sign_catalog.py --all``. No Python touched.

The HMAC key in CI is injected via ``NORA_OID_CATALOG_SIGNING_KEY``;
passing ``--key`` overrides the env var so a developer can sign a
local fixture without exporting secrets.

Public object names only — see the ``oids`` map in the source JSON. No
MIB prose is embedded; OIDs are dotted strings sourced from live SNMP
walks against Cambium PMP 450i hardware (issue #35).

Examples:

    # Default: read sources/cambium/pmp450i/15.2.1.source.json and write
    # the signed envelope into data/oid-catalogs/cambium/pmp450i/15.2.1.json.
    NORA_OID_CATALOG_SIGNING_KEY="change-me" python scripts/sign_catalog.py

    # Sign every source file in the source root at once.
    python scripts/sign_catalog.py --all

    # Sign a single triple into a custom output root.
    NORA_OID_CATALOG_SIGNING_KEY="change-me" \\
        python scripts/sign_catalog.py --vendor cambium --model pmp450i \\
        --firmware 15.3.0 --output-root /tmp/catalogs

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
# Default source root: the editable ``*.source.json`` files live here.
# Operators / contributors update these by adding new firmware drops or
# fixing OIDs against real-radio SNMP walks.
DEFAULT_SOURCE_ROOT = REPO_ROOT / "data" / "oid-catalogs" / "sources"
# Default output root: signed envelopes (HMAC-verified at boot) live
# here. PR 1's built-in baseline ships from
# ``src/nora/data/oid-catalogs/`` (signed out-of-band with the baseline
# key) and the runtime operator root defaults here for re-sign tooling.
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "data" / "oid-catalogs"


# Per-tool OID-name map (slices 2/3/4 of `2026-09-13-pmp450i-production-surface`).
#
# The catalog envelope carries this map so the boot-time guard in
# ``OidCatalogRegistry._enumerate_tool_names`` can build the
# ``REQUIRED_OIDS_BY_TOOL`` index. The keys are MCP tool names
# registered on the global ``FastMCP("nora")`` instance; the values
# are OID names (drawn from the source JSON's ``oids`` map) the tool
# fetches per call.
#
# This is *schema*, not *data* — it stays in code because every
# addition here MUST land alongside the matching ``@mcp.tool`` body in
# ``src/nora/server.py``. Adding a new firmware does NOT require
# touching this map (the OID values are data; the tool → OID mapping
# is structural).
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
    "snmp_migrate_radio_frequency": [
        "migrateCarrierFrequency",
        "migratePriorCarrierFrequency",
    ],
    # Issue #42 / `2026-09-15-register-device-mcp`: `register_device`
    # carries the cheapest possible reachability probe (`sysDescr` GET
    # against `1.3.6.1.2.1.1.1.0`). The envelope entry retires the
    # legacy `_ALLOWED_UNCATALOGUED_TOOLS` allow-list entry in the
    # same PR (Task 7).
    "register_device": ["sysDescr"],
    # Slice-1 radio-metrics tool — promoted from the legacy
    # `_ALLOWED_UNCATALOGUED_TOOLS` allow-list. Same OID-set as the
    # legacy driver path: `report_firmware` reads `sysDescr` and the
    # other five radio-metrics OIDs. Re-signing the catalog with this
    # envelope retires the explicit allow-list entry in `server.py:626`.
    "snmp_get_pmp450i_radio_metrics": [
        "radioDownlinkRate",
        "radioUplinkRate",
        "signalStrengthRx",
        "signalStrengthTx",
        "ssr",
        "modulationMode",
        "sysDescr",
    ],
}


def _load_source(source_path: Path) -> dict[str, object]:
    """Load and validate a source JSON file.

    The schema is minimal: a top-level object with ``vendor``, ``model``,
    ``firmware`` (strings), and ``oids`` (object → dotted OID strings).
    An optional ``_provenance`` block documents the verification source
    for audit (it's preserved verbatim in the signed envelope as
    informational metadata but excluded from the HMAC body).

    An optional ``version`` field (positive integer, default 1) bumps
    the envelope's ``version`` field. The ODD cut coordinated on
    2026-09-18 (feature ``feat/multi-community-band-reboot``) ships
    ``version: 2`` for every signed envelope because the
    ``frequency`` / ``migrateCarrierFrequency`` / ``migratePriorCarrierFrequency``
    OIDs were re-pointed from the deprecated ``rfFreqCarrier``
    (`1.3.6.1.4.1.161.19.3.1.1.2.0`) to the current ``radioFreqCarrier``
    (`1.3.6.1.4.1.161.19.3.1.10.1.1`).
    """
    payload = json.loads(source_path.read_text())
    if not isinstance(payload, dict):
        raise ValueError(f"{source_path}: source must be a JSON object")
    for key in ("vendor", "model", "firmware", "oids"):
        if key not in payload:
            raise ValueError(f"{source_path}: source missing required field {key!r}")
    if not isinstance(payload["oids"], dict):
        raise ValueError(f"{source_path}: source.oids must be a JSON object")
    if "version" in payload and not isinstance(payload["version"], int):
        raise ValueError(f"{source_path}: source.version must be an integer")
    if "version" in payload and payload["version"] < 1:
        raise ValueError(f"{source_path}: source.version must be >= 1")
    return payload


def _sign_one(
    *,
    source_path: Path,
    output_root: Path,
    key: str,
) -> Path:
    """Sign one source JSON and write the envelope under ``output_root``.

    Returns the path of the written envelope.
    """
    payload = _load_source(source_path)
    vendor = str(payload["vendor"])
    model = str(payload["model"])
    firmware = str(payload["firmware"])
    oids_raw = payload["oids"]
    assert isinstance(oids_raw, dict)
    oids: dict[str, str] = {str(k): str(v) for k, v in oids_raw.items()}
    # ``version`` is read from the source when present; default 1 keeps
    # back-compat with v1 source files that predate the version field.
    envelope_version = int(payload.get("version", 1))

    canonical_body = json.dumps(oids, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key.encode("utf-8"), canonical_body, hashlib.sha256).hexdigest()
    envelope = {
        "version": envelope_version,
        "vendor": vendor,
        "model": model,
        "firmware": firmware,
        "oids": oids,
        # The envelope carries a per-tool OID-name map so the boot-time
        # guard can build ``REQUIRED_OIDS_BY_TOOL`` at boot. The HMAC
        # is computed over the canonicalised ``oids`` map only — the
        # tools map is informational and excluded from the signature
        # to keep back-compat with the v1 verification path.
        "tools": TOOLS_V1,
        "hmac_sha256": sig,
    }
    # ``_provenance`` is preserved verbatim from the source JSON when
    # present, so the audit trail (live walk date, MIB references, notes)
    # ships with the signed envelope. Excluded from the HMAC body so
    # the signature stays tooling-stable across provenance edits.
    if "_provenance" in payload:
        envelope["_provenance"] = payload["_provenance"]

    target = output_root / vendor / model / f"{firmware}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(envelope, indent=2))
    print(f"wrote {target} ({len(oids)} OIDs)")
    return target


def _iter_source_files(source_root: Path) -> list[Path]:
    """Yield every ``*.source.json`` under ``source_root``, sorted.

    Iteration is deterministic (sorted at every level) so the
    ``--all`` batch is reproducible across hosts.
    """
    if not source_root.is_dir():
        return []
    return sorted(source_root.rglob("*.source.json"))


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sign an OID catalog envelope from a source JSON file and "
            "write it under --output-root. Defaults read "
            "data/oid-catalogs/sources/cambium/pmp450i/15.2.1.source.json "
            "and write data/oid-catalogs/cambium/pmp450i/15.2.1.json."
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
        "--source-root",
        type=Path,
        default=DEFAULT_SOURCE_ROOT,
        help=(
            "Root directory holding the editable ``*.source.json`` files. "
            "Defaults to <repo>/data/oid-catalogs/sources/. "
            "Used by --all to enumerate every source for batch signing."
        ),
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=(
            "Path to a single source JSON file. Overrides "
            "--vendor/--model/--firmware resolution when present."
        ),
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="sign_all",
        help=(
            "Sign every ``*.source.json`` file under --source-root in one "
            "pass. Recommended workflow when a new firmware drop lands."
        ),
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=DEFAULT_OUTPUT_ROOT,
        help=(
            "Root directory the signed envelopes are written under. "
            "Defaults to <repo>/data/oid-catalogs/."
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

    if args.source is not None:
        if not args.source.is_file():
            print(f"error: --source {args.source} does not exist", file=sys.stderr)
            return 2
        _sign_one(source_path=args.source, output_root=args.output_root, key=key)
        return 0

    if args.sign_all:
        sources = _iter_source_files(args.source_root)
        if not sources:
            print(
                f"error: no *.source.json files found under {args.source_root}",
                file=sys.stderr,
            )
            return 2
        for source_path in sources:
            _sign_one(source_path=source_path, output_root=args.output_root, key=key)
        return 0

    # Single-triple mode: resolve the source file under --source-root.
    source_path = (
        args.source_root
        / args.vendor
        / args.model
        / f"{args.firmware}.source.json"
    )
    if not source_path.is_file():
        print(
            f"error: source file not found at {source_path}. "
            f"Either drop the source JSON there, pass --source <path>, "
            f"or run with --all to sign every source under --source-root.",
            file=sys.stderr,
        )
        return 2
    _sign_one(source_path=source_path, output_root=args.output_root, key=key)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
