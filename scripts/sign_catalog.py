"""Generate the shipped PMP 450i v1 OID catalog fixture (HMAC-signed).

This script is a one-shot helper that signs the public-name OID map and
writes the envelope into ``data/oid-catalogs/cambium/pmp450i/15.2.1.json``.
Re-run it whenever the catalog list changes; the HMAC key in CI is
injected via ``NORA_OID_CATALOG_SIGNING_KEY``.

Public object names only — see ``OID_CATALOG_V1`` below. No MIB prose
is embedded; OIDs are dotted strings sourced from Cambium public
documentation and operator community forums (never from the WHISP-SM-MIB
text, which is EULA-restricted).

Run with:

    NORA_OID_CATALOG_SIGNING_KEY="change-me" python scripts/sign_catalog.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TARGET = REPO_ROOT / "data" / "oid-catalogs" / "cambium" / "pmp450i" / "15.2.1.json"

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


def main() -> int:
    key = os.environ.get("NORA_OID_CATALOG_SIGNING_KEY")
    if not key:
        print("error: NORA_OID_CATALOG_SIGNING_KEY is required", file=sys.stderr)
        return 2

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    canonical_body = json.dumps(OID_CATALOG_V1, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(key.encode("utf-8"), canonical_body, hashlib.sha256).hexdigest()
    envelope = {
        "version": 1,
        "vendor": "cambium",
        "model": "pmp450i",
        "firmware": "15.2.1",
        "oids": OID_CATALOG_V1,
        "hmac_sha256": sig,
    }
    TARGET.write_text(json.dumps(envelope, indent=2))
    print(f"wrote {TARGET} ({len(OID_CATALOG_V1)} OIDs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
