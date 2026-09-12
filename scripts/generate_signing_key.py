"""Generate a cryptographically-random NORA catalog signing key.

The signing key secures OID catalog integrity (HMAC-SHA256 at boot — see
`openspec/specs/oid-catalog/spec.md` Requirement "HMAC-SHA256 Boot
Verification"). A weak key collapses that defense into a single-line
tamper vector, so this script:

- Sources randomness from `secrets.token_urlsafe`, which uses the OS CSPRNG
  (`/dev/urandom` on POSIX, `BCryptGenRandom` on Windows). NOT `random`,
  which is a Mersenne Twister and is NOT cryptographically secure.
- Defaults to 32 bytes → ~256 bits of entropy, encoded as 43 URL-safe
  base64 chars. This matches HMAC-SHA256's block size and is well above
  any practical brute-force threshold.
- Prints exactly one line to stdout so the output is shell-pipeable:
  `NORA_OID_CATALOG_SIGNING_KEY=$(python scripts/generate_signing_key.py)`

NEVER commit a generated key. NEVER paste it into a chat. NEVER log it.
Store it in your secret manager of choice (vault, age-encrypted file,
systemd `EnvironmentFile=` with mode 0600, etc.) — see OPERATIONS.md.
"""
from __future__ import annotations

import secrets


def main() -> int:
    """Print one URL-safe-base64 random key to stdout."""
    print(secrets.token_urlsafe(32))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
