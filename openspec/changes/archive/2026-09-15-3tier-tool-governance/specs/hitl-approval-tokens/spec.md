# hitl-approval-tokens Specification

## Purpose

Cryptographic lifecycle of Tier-2 `HitlApprovalToken` objects:
HMAC-SHA256 mint, constant-time verify, legacy-stub rejection. Closes
the forgeability gap surfaced in `explore.md` §"CRITICAL GAP".

## Reconciliation

**NEW**. No prior spec at `openspec/specs/hitl-approval-tokens/`;
implementation at `src/nora/hitl/tokens.py`. User brief's name
retained.

## Frozen Canonical Payload (proposal §"HMAC approach")

```
f"{operator_id}|{issued_at_iso8601}|{expires_at_iso8601}|{token}"
```

`|`-joined tuple (NOT a JSON dict), UTF-8 encoded before HMAC.

## Requirements

### Requirement: HMAC-SHA256 Mint Signature

`mint_token(operator_id, *, ttl_seconds=900)` SHALL compute the
canonical payload, UTF-8 encode it, and sign with
`hmac.new(key_bytes, canonical_body, hashlib.sha256).hexdigest()`.
The returned `HitlApprovalToken` SHALL include a non-empty
`signature: str = Field(min_length=1)`.

#### Scenario: mint produces a typed model with a non-empty signature

- GIVEN `Settings.nora_hitl_signing_key` is a non-empty `SecretStr`
  AND `Settings.nora_hitl_token_ttl_seconds == 900`
- WHEN `mint_token("alice")` runs
- THEN a frozen `HitlApprovalToken` is returned AND
  `len(token.signature) >= 1` AND `token.operator_id == "alice"`

#### Scenario: canonical payload uses the frozen tuple format

- GIVEN any successful `mint_token` call
- WHEN the canonical payload is reconstructed from the model
- THEN it equals
  `f"{operator_id}|{issued_at_iso8601}|{expires_at_iso8601}|{token}"`
  verbatim (`|` separator, NOT a JSON dict)

### Requirement: Constant-Time Verify With `hmac.compare_digest`

`verify_approval_token(token: str | None) -> HitlApprovalToken` SHALL
recompute the canonical payload, recompute the HMAC, and compare with
`hmac.compare_digest`. The verifier MUST NOT use `==` on the
signature; this prevents timing oracles against the operator's
signing key.

#### Scenario: legitimate token verifies with compare_digest

- GIVEN a token freshly minted by `mint_token("alice")` with key `K`
- WHEN `verify_approval_token(serialised_token)` runs
- THEN the typed `HitlApprovalToken` is returned AND no exception is
  raised

#### Scenario: verifier uses compare_digest (timing-attack resistance)

- GIVEN `verify_approval_token` source
- WHEN the source is statically scanned for the comparison operator
- THEN `hmac.compare_digest(...)` is present AND `==` is absent from
  any signature-comparison path

### Requirement: Tampered Payload Rejection

Any mutation to canonical-payload inputs (`operator_id`,
`issued_at`, `expires_at`, or `token`) SHALL cause
`verify_approval_token` to raise `AutonomousMutationRejected` with
the **literal** message
`"autonomous device mutation rejected: HITL approval token required"`
(pinned by
`tests/test_hitl_tokens.py::migrate_autonomous_call_raises_autonomous_mutation_rejected`;
MUST NOT change).

#### Scenario: mutated operator_id fails verification

- GIVEN a minted token under operator `"alice"`
- WHEN the operator-id field is rewritten to `"mallory"` and
  `verify_approval_token` runs
- THEN `AutonomousMutationRejected` is raised AND the message equals
  the literal byte-for-byte

#### Scenario: mutated expires_at fails verification

- GIVEN a minted token with `expires_at = now + 900s`
- WHEN `expires_at` is rewritten to `now + 9999s` and verification
  runs
- THEN `AutonomousMutationRejected` is raised with the literal
  message AND the original signature does NOT match the recomputed
  HMAC

### Requirement: Legacy Stub Token Rejection

Prior stub tokens (`stub-<op>-<unix>` JSON, no `signature` field)
SHALL be rejected. The Pydantic model declares
`signature: str = Field(min_length=1)`; missing fields fail schema
validation → `AutonomousMutationRejected` with the literal message.
**No silent acceptance.**

#### Scenario: stub token without signature fails verification

- GIVEN a legacy stub payload with no `signature` key (or empty
  signature)
- WHEN `verify_approval_token(stub_payload)` runs
- THEN `AutonomousMutationRejected` is raised AND the literal
  message appears

#### Scenario: no dual-verify window (hard-break default)

- GIVEN pre-deploy stub tokens
- WHEN replayed against the new verifier
- THEN every replay raises `AutonomousMutationRejected` (legacy
  tokens invalidated at deploy time)

### Requirement: Signing Key Sourced From Settings

The HMAC key SHALL be sourced from
`Settings.nora_hitl_signing_key: SecretStr`. Boot SHALL fail closed
with a typed error if the key is empty **and a Tier-2 tool is
invoked** (mirrors `OidCatalogRegistry`). Tests inject a hermetic
key.

#### Scenario: missing signing key fails closed on Tier-2 invocation

- GIVEN `Settings.nora_hitl_signing_key` is empty
- WHEN `snmp_migrate_radio_frequency(...)` is invoked
- THEN a typed boot/verification error is raised AND the literal
  `AutonomousMutationRejected` message is preserved

#### Scenario: key derived from SecretStr is byte-stable

- GIVEN `Settings.nora_hitl_signing_key.get_secret_value()` returns
  `K`
- WHEN `mint_token("alice")` signs with `K` AND
  `verify_approval_token` recomputes with `K`
- THEN `hmac.compare_digest` returns `True`

## Open Questions (deferred to design)

**Q2** — back-compat window. **Q5** — key rotation (flag for
Phase-3).

## Cross-References

`pmp450i-radio-tools` (literal pinned), `nora-mcp-server`
(`nora hitl mint`), `secure-configuration` (`nora_hitl_signing_key`).