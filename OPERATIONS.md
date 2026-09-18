# Operating NORA MCP

Day-2 operations: key management, updates, log interpretation, integration
contracts, troubleshooting. Read INSTALL.md first if the service is not
yet running.

## Test execution workflow

Three execution modes, each tuned for a different loop. Pick the one that
matches what you're doing — running the wrong mode is the single most
common source of slow feedback.

### Dev loop (fast iteration)

Use when editing code or a single test file. Parallel, no coverage, no
cache provider overhead. The full suite lands in ~25–35 s on a multi-core
workstation; a single-file run lands under 1 s on the second invocation.

```bash
# Whole suite, parallel, no coverage. Skips tests marked `no_xdist`
# (pre-existing flakes on /tmp/nora-bootstrap-* and TCP port 8765); they
# still run in `make test` (CI sequential).
make test-fast

# One test by name pattern (substring match against test IDs / names)
make test-one K=hitl_tokens
make test-one K=snmp_pmp450i::test_register_device

# Auto-rerun on file changes (uses pytest-watch under the hood)
make watch
```

### Pre-commit / pre-PR (full suite with coverage)

Use before opening a PR or after finishing a feature. Adds coverage
measurement and the strict-markers / cacheprovider behavior the default
`addopts` skips.

```bash
make test   # full pytest + coverage report in the terminal
```

### CI (GitHub Actions)

`/.github/workflows/ci.yml` runs on every push and PR to `main`. Caches
two layers — uv's wheel cache (key = `hashFiles('uv.lock')`) and the
materialized `.venv` (key = `hashFiles('uv.lock') + hashFiles('pyproject.toml')`)
— so a cache hit skips `uv sync` entirely. Pre-compiles bytecode before
pytest so the first import inside the runner doesn't pay the cold-path
cost. In-progress runs on the same ref are auto-cancelled.

If CI is slow on a cache miss, that is expected; the second run is the
steady state.

## Key management

The signing key (`NORA_OID_CATALOG_SIGNING_KEY`) is the root of trust for
every catalog file NORA loads. It authenticates the OID mappings that the
SNMP driver uses to talk to your radios; a leaked key lets an attacker
pin forged OIDs that NORA will accept as legitimate. Treat it as you would
treat any root credential.

### Generate

```bash
.venv/bin/python scripts/generate_signing_key.py
```

The output is `secrets.token_urlsafe(32)` — 32 bytes from the OS CSPRNG,
URL-safe-base64 encoded. ~256 bits of entropy. Never use a password, a
short string, or `random.choice` for this — `secrets` is the only Python
API that draws from a cryptographically secure source.

### Store

Choose ONE of the following, in order of preference:

1. **Secret manager** (HashiCorp Vault, AWS Secrets Manager, Google Secret
   Manager, age-encrypted file in git). The service account reads the key
   at boot via a fetch step in `ExecStartPre=`.
2. **systemd `EnvironmentFile=`** with file mode `0600`, owned by the
   service user (`nora:nora`). This is the configuration baked into the
   bundled unit file.
3. **Process environment** exported by a shell wrapper. Acceptable for
   development, NOT acceptable for production — process environments are
   visible to other processes owned by the same user.

The bundled `scripts/nora-mcp.service` reads from
`EnvironmentFile=/etc/nora/nora.env` with the assumption that the file
contains every variable listed in `.env.example`. The file MUST NOT be
world-readable; `chmod 0640` or `chmod 0600` and `chown nora:nora`.

### Rotate

Rotation is required when:

- The key has been exposed (logged, committed, pasted in chat).
- An operator with access leaves the team.
- The schedule mandates it (recommended: every 12 months).

The catalog contract (see `openspec/specs/oid-catalog/spec.md`
Requirement "Key Rotation Behaviour") is: **old keys do not accept old
catalogs, new keys do not accept old catalogs**. Rotation is therefore a
two-phase operation:

**Phase 1 — generate the new key and re-sign every catalog**

```bash
NEW_KEY=$(.venv/bin/python scripts/generate_signing_key.py)
NORA_OID_CATALOG_SIGNING_KEY="$NEW_KEY" .venv/bin/python scripts/sign_catalog.py
# Repeat for any additional vendor/model/firmware catalogs under data/oid-catalogs/
```

Commit the re-signed catalogs. Do NOT commit the new key.

**Phase 2 — roll the key out**

For a single-host install, update `/etc/nora/nora.env` and restart:

```bash
sudo sed -i "s|^NORA_OID_CATALOG_SIGNING_KEY=.*|NORA_OID_CATALOG_SIGNING_KEY=$NEW_KEY|" /etc/nora/nora.env
sudo systemctl restart nora-mcp
sudo journalctl -u nora-mcp -n 5    # verify "nora-mcp boot complete"
```

For a multi-host install, there is a brief window during which some hosts
hold the old key and some hold the new. During that window, the new hosts
will reject the old catalogs and fail to boot. To avoid downtime, do a
rolling restart with the old key still valid:

1. Re-sign the catalogs with the new key on every host (each host has its
   own copy of the catalog under `data/oid-catalogs/`, or all hosts share
   the catalog via a synced path).
2. Restart host 1 → confirm boot.
3. Repeat for each host.

If the catalogs are managed centrally (e.g. via a config-management repo
that ships signed envelopes), there is no need for a window: cut over the
catalog repo first (everyone still has the old key but reads the new
catalog and FAILS to boot), then cut over the keys (everyone has the new
key and reads the new catalog and BOOTS). Plan a brief coordinated
restart.

### Revoke

There is no revocation list — NORA does not phone home. Rotation IS
revocation: any host that does not have the new key will refuse every
catalog at the next boot. The blast radius of a leaked key is therefore
bounded by how quickly you can rotate.

## Update procedure

```bash
cd /opt/nora
sudo systemctl stop nora-mcp
git pull
uv sync
.venv/bin/python scripts/sign_catalog.py    # re-sign if the catalog changed
sudo systemctl start nora-mcp
sudo journalctl -u nora-mcp -n 20
```

If `uv.lock` changed (new dependency pins), `uv sync` resolves and
installs in one step. If only the catalog changed, `sign_catalog.py` is
the only post-pull action needed.

## Log interpretation

Every tool invocation emits one structured INFO line on stderr:

```
2026-09-12 10:12:45,329 INFO nora.cli nora-mcp boot complete: catalogs=/opt/nora/data/oid-catalogs/ devices=12
[09/12/26 10:12:45] INFO     Starting MCP server 'nora' with transport 'stdio'
2026-09-12 10:12:45,103 INFO mcp.server.lowlevel.server Processing request of type ListToolsRequest
2026-09-12 10:12:45,107 INFO mcp.server.lowlowlevel.server Processing request of type CallToolRequest
2026-09-12 10:12:45,114 INFO nora.server tool=search_intervention_history duration_ms=6 outcome=success
```

What to look for:

| Pattern                                        | Meaning                                                          |
|------------------------------------------------|------------------------------------------------------------------|
| `nora-mcp boot complete: catalogs=... devices=N` | Healthy boot. `devices=N` should match `len(devices.yaml)`.     |
| `tool=<name> duration_ms=<int> outcome=success` | One per MCP tool call. `duration_ms` is a useful load signal.    |
| `tool=<name> duration_ms=<int> outcome=error`   | The tool raised. Pair with `journalctl -p err` to see the trace. |
| `intervention_memory: keyword search cap reached: cap=N records_read=N` | The operator should raise `NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS` or narrow the query. |
| `intervention_memory: correlate scan cap reached: cap=N records_read=N` | Same as above, for `NORA_INTERVENTIONS_CORRELATE_SCAN_LIMIT`. |
| `CatalogVerificationError`                      | HMAC mismatch. STOP — do not bypass. See Troubleshooting.        |
| Traceback ending in `OidCatalogRegistry.verify_all` | Boot aborted because of catalog verification failure.           |

### Transport modes

The boot log line tells you which transport the daemon bound. Look for
the `Starting MCP server 'nora' with transport '...'` line emitted by
FastMCP right after the NORA `nora-mcp boot complete:` line.

| Mode            | Boot log line                                                                       | How to enable                                                    |
|-----------------|-------------------------------------------------------------------------------------|------------------------------------------------------------------|
| `stdio` (default) | `Starting MCP server 'nora' with transport 'stdio'`                              | No config. The systemd `ExecStart=/opt/nora/.venv/bin/nora-mcp` runs stdio. |
| `http`          | `Starting MCP server 'nora' with transport 'http' on http://127.0.0.1:8005/mcp`     | Set `NORA_MCP_TRANSPORT=http` (and optionally `_HOST`, `_PORT`, `_PATH`) in `/etc/nora/nora-mcp.env`; `sudo systemctl restart nora-mcp`. |
| `streamable-http` | `Starting MCP server 'nora' with transport 'streamable-http' on ...`             | Set `NORA_MCP_TRANSPORT=streamable-http` in `/etc/nora/nora-mcp.env`. Modern MCP alias for `http`. |
| `sse` (legacy)  | `Starting MCP server 'nora' with transport 'sse' on http://127.0.0.1:8005/sse`     | Set `NORA_MCP_TRANSPORT=sse` in `/etc/nora/nora-mcp.env`. Incompatible with `NORA_MCP_STATELESS_HTTP=true`. |

`scripts/verify-install.sh --check-http` adds an opt-in HTTP probe for
non-stdio installs: it TCP-probes `127.0.0.1:$NORA_MCP_PORT` and
(optionally, when `curl` is on PATH) GETs `$NORA_MCP_PATH`. Default
mode is unchanged for stdio operators — no regression.

## Integration contract: NORA ↔ OpenChat

NORA and OpenChat share one directory. The contract is one-way data flow:

```
OpenChat writer          →         shared directory         ←       NORA reader
(intervention_memory_tool /        NORA_INTERVENTIONS_DIR          (3 read-only tools)
 cambium_pmp450_tool               (JSON files)
 _auto_save_memory)
```

NORA never writes to the directory. The hard read-only rule is enforced
structurally by `tests/intervention_memory/test_no_writes.py` (AST scan
of the `intervention_memory` package). Any NORA PR that adds a write
path to that directory fails CI.

### Record schema

NORA reads records written by OpenChat's `intervention_memory_tool.save_intervention_record`
and `cambium_pmp450_tool._auto_save_memory`. The schema is defined by
`src/nora/intervention_memory/models.py`:

| Field                          | Type     | Required | Notes                                                                       |
|--------------------------------|----------|----------|-----------------------------------------------------------------------------|
| `intervention_id`              | str      | yes      | `INT-<ticket>-<ip>-<unix>-<6hex>`                                           |
| `timestamp_iso`                | str      | yes      | ISO-8601 UTC                                                                |
| `timestamp_unix`               | int      | yes      | Unix epoch seconds                                                          |
| `ticket_number`                | str      | yes      | Alphanumeric; `-` and `_` allowed                                           |
| `target_ip`                    | str      | yes      | IPv4 or IPv6 literal                                                        |
| `stage`                        | literal  | yes      | `PRE_DIAGNOSTIC` / `SPECTRUM_ANALYSIS` / `PRE_MIGRATION` / `SAFETY_ABORT` / `POST_MIGRATION_VERIFIED` / `POST_INTERVENTION` |
| `record_name`                  | str      | yes      | Free text — sanitized on read                                               |
| `status`                       | literal  | yes      | `COMPLETED` / `ABORTED` / `ACTION_REQUIRED` / `PENDING_VERIFICATION`         |
| `agent_name`                   | str      | yes      | Free text — sanitized on read                                               |
| `network_equipment`            | object   | yes      | See below                                                                  |
| `findings_and_dictamen`        | str      | yes      | Free text — sanitized on read                                               |
| `created_at`                   | str      | yes      | ISO-8601 UTC                                                                |
| `recommended_action`           | str      | no       | Free text — sanitized on read                                               |

`network_equipment` sub-schema (every field optional):

| Field                                 | Type   | Notes                                          |
|---------------------------------------|--------|------------------------------------------------|
| `target_ip`                           | str    | Mirrors top-level for convenience              |
| `system_name`                         | str    | Tower / AP name — sanitized on read            |
| `hardware_band`                       | str    | `5 GHz` / `3 GHz`                              |
| **`carrier_frequency_mhz`**           | float  | **Canonical field read by `correlate_sector_interference`** |
| `total_provisioned_sms`               | int    |                                                |
| `active_online_sms_count`             | int    |                                                |
| `pre_existing_offline_sms_count`      | int    |                                                |
| `frame_utilization_dl_pct`            | float  |                                                |
| `frame_utilization_ul_pct`            | float  |                                                |
| `pre_existing_offline_subscribers`    | list   | One entry per known-offline SM                 |

**Critical**: `correlate_sector_interference` reads ONLY the canonical
field `carrier_frequency_mhz`. OpenChat historically wrote
`verified_carrier_frequency_mhz` on POST_MIGRATION_VERIFIED records;
that field is silently ignored by correlate. The fix lives in
OpenChat's `deploy_v7_intervention_memory.py` and
`deploy_v8_unbiased_pre_report.py` (one-line addition each). The contract
regression is locked by
`tests/intervention_memory/test_openchat_writer_contract.py` in NORA —
running NORA tests on a fresh checkout will catch any future openchat
deploy script that drops the canonical field again.

### Sharing the directory

Three options, in order of operational simplicity:

1. **NFS mount** at `/var/lib/nora/interventions/`. Easiest if both
   hosts are on the same datacenter.
2. **S3-fuse / object-storage FUSE** (s3fs, goofys, rclone mount).
   Works across datacenters and for HA.
3. **Periodic `rsync`** from the OpenChat host to the NORA host. Add a
   cron job every 30s and accept up-to-30s staleness on read.

Latency matters less than durability: a write that survives a host
crash is more important than one that propagates in <1 second. NFS and
FUSE both give POSIX consistency; rsync gives eventual consistency.

## Troubleshooting

| Symptom (stderr)                                                                    | Likely cause                                                                | Fix                                                                                                  |
|--------------------------------------------------------------------------------------|-----------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| `CatalogVerificationError: missing signing key (NORA_OID_CATALOG_SIGNING_KEY is empty)` | `.env` not loaded, or key variable is empty                                | Confirm `/etc/nora/nora.env` contains the line; reload systemd: `systemctl daemon-reload && systemctl restart nora-mcp` |
| `CatalogVerificationError: ... HMAC-SHA256 signature mismatch`                        | Key changed; catalog not re-signed, OR catalog was tampered                | Re-sign: `sudo -u nora -E .venv/bin/python scripts/sign_catalog.py`. If `git diff data/oid-catalogs/` shows unexpected changes, the file was modified — restore from a known-good source. |
| `CatalogVerificationError: ... missing required OID(s): modulationMode, ...`         | Catalog file is the wrong shape (e.g. empty, or from a different vendor) | Re-run `sign_catalog.py`; if the script does not regenerate the file, the upstream OID_CATALOG_V1 in the script was edited — pull a fresh `scripts/sign_catalog.py` from the repo. |
| `FileNotFoundError: data/devices.yaml`                                                | `NORA_DEVICES_INVENTORY_PATH` points at a file that does not exist         | Confirm the path; the directory of the path must be readable by the `nora` user.                      |
| `ValidationError: ... vendor Field required`                                          | `devices.yaml` is missing required fields                                   | See INSTALL.md § 1 for the device schema (`vendor`, `model`, `firmware`, `host`, `snmp_version`, `community` or v3 credentials). |
| `intervention_memory: keyword search cap reached`                                    | Operator ran a broad keyword query against a large interventions dir       | Either narrow the query, raise `NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS`, or partition the directory by date. |
| `correlate_sector_interference` returns zero conflicts for a tower you know has carriers | OpenChat POST_MIGRATION records are missing `carrier_frequency_mhz`     | Re-run OpenChat's `deploy_v7_intervention_memory.py` against the production `webui.db` so the new shape overwrites old records. |
| systemd unit fails with `status=203/EXEC`                                             | The venv path in `ExecStart=` is wrong                                     | `ls -l /opt/nora/.venv/bin/nora-mcp`; if missing, re-run `uv sync`.                                 |
| `DeprecationWarning: python -m nora is deprecated`                                    | Something invoked the legacy alias                                         | Use `nora-mcp` instead. The alias is kept only for backward compatibility.                            |

## Tier-1 operator-clearance gate (issue #43)

`snmp_run_spectrum_analysis` now requires `operator_confirmed=True` on the
wire. The default is `False` (fail-closed); the server-side gate raises
`Tier1ClearanceRequired` BEFORE any SNMP GET is emitted when the flag
is False or absent. The Tier-1 protocol precedes the maintenance-window
check (highest-priority invariant) — a confirmed clearance does NOT
bypass the window check.

| Tool tier | Wire contract | Failure mode |
|-----------|---------------|--------------|
| Tier 0    | No special clearance. Direct execution. | Standard typed errors. |
| Tier 1    | `operator_confirmed: bool = False` (the default). | `Tier1ClearanceRequired` raised BEFORE any wire frame when False / absent. |
| Tier 2    | `approval_token: str` carrying an HMAC-SHA256-signed `HitlApprovalToken`. | `AutonomousMutationRejected` raised with literal message `autonomous device mutation rejected: HITL approval token required`. |

## HITL signing-key management (`NORA_HITL_SIGNING_KEY`)

`Settings.nora_hitl_signing_key: SecretStr` is the HMAC-SHA256 signing key
for HITL approval tokens (`nora hitl mint`). Mirrors the catalog signing
key management above (same `SecretStr` storage, same generation script).

**Rotation is DEFERRED to Phase-3 (issue #43 out-of-scope note).** Until
Phase-3 lands, rotation is operator-managed:

1. Generate a fresh key with `.venv/bin/python scripts/generate_signing_key.py`.
2. Update `/etc/nora/nora.env` (or your secret manager) with the new value.
3. Restart NORA: `sudo systemctl restart nora-mcp`.

Restarting invalidates every in-flight token because the new HMAC key
will not match the signature previously produced. This is acceptable
for Phase-2 because operator sessions are typically short and a token
re-mint takes one CLI invocation (`nora hitl mint`). Phase-3 will
introduce a multi-key rotation story that supports overlapping validity
windows.

### Hard-break legacy stub tokens

The previous stub token format (`stub-<operator-id>-<unix>` JSON, no
`signature` field) is **invalidated at deploy time**. There is no
dual-verify window — every stub token raises
`AutonomousMutationRejected` with the literal message
`autonomous device mutation rejected: HITL approval token required`.

The previous kill switch (`NORA_HITL_TOKEN_TTL_SECONDS=0`) remains
effective as an operational backout: setting the env var to `0`
rejects every token (legitimate or stub) at `verify_approval_token`
time.

### `nora hitl mint` — operator-facing CLI

```bash
NORA_HITL_SIGNING_KEY=<key> nora hitl mint --operator-id alice --ttl-seconds 900
# {"token": "hitl-alice-...", "operator_id": "alice", "issued_at": "...",
#  "expires_at": "...", "signature": "<hmac_sha256_hex>"}
```

The signed token is printed as a single JSON line on stdout. Pipe it
into the migration tool's `approval_token` parameter, or store it in a
secret manager for programmatic use. Exit code 0 on success; 2 on
missing `--operator-id` or empty `NORA_HITL_SIGNING_KEY`.

## 3-tier tool governance taxonomy (issue #43)

Every NORA MCP tool is classified into one of three Service-Impact
Tiers per the `tool-service-impact-tiers` capability. The full per-tool
spec lives under `docs/tool_specs/<tool_name>.md`; the orchestrator
prompt at `src/nora/prompts/netops_orchestrator.md` carries §6
"Universal Service Impact & Disruption Gate" which references every
tier and the clearance / HITL protocol for each.

| Tier | Category | Policy | Tools |
|------|----------|--------|-------|
| 0 | Passive Telemetry (Read-Only) | Direct execution | 8 tools (AP summary, SM table, radio metrics, frame util, SM diagnostics, intervention history search, device lifecycle, sector correlation) |
| 1 | Potentially Disruptive / Active Telemetry | Pause & Clearance Gate (`operator_confirmed=True`) | 1 tool (`snmp_run_spectrum_analysis`) |
| 2 | Service-Affecting Mutations (Write / Config) | Strict HITL Gate (`approval_token` + HMAC verification + make-before-break) | 2 tools (`snmp_migrate_radio_frequency`, `save_intervention_record`) |

The boot sequence loads both the packaged prompts directory
(`src/nora/prompts/`) AND the tool-spec directory
(`docs/tool_specs/` — overridable via `NORA_TOOL_SPECS_DIR`). Each
tool-spec file carries YAML front-matter with the frozen ADR-4
schema; the cross-validator at scan time enforces
`{tier 1 ⇒ requires_operator_confirmed=True}`,
`{tier 2 ⇒ requires_hitl_token=True}`, and
`{tier ∈ {0, 1, 2}}`.
