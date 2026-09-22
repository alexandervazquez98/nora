# Operating NORA MCP

Day-2 operations: key management, updates, log interpretation, integration
contracts, troubleshooting. Read INSTALL.md first if the service is not
yet running.

## For developers

If you're contributing code or tests, start with
[`[CONTRIBUTING.md]`](CONTRIBUTING.md). It covers the test execution
workflow (dev loop / pre-PR / CI), when to use shared fixtures vs
inline subprocess, how to mark flaky-under-xdist tests, and the
fixtures recipe (`scope="session"` + `worker_id`, fixed env dict, not
`**os.environ`).

For the architectural rationale of the testing strategy (xdist
parallelism opt-in, session-scoped subprocess fixtures, `no_xdist`
marker), see [`[ADR-0002]`](docs/adr/0002-testing-strategy.md).

The sections below are operator-facing (install, deploy, key management,
OpenChat integration). Keep them focused on the production deployment
contract, not on development workflow.

---

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

**Recommended path** (headless, idempotent, with automatic rollback):

```bash
sudo nora upgrade
```

Pin a specific version:

```bash
sudo nora upgrade --ref v0.3.8          # by tag
sudo nora upgrade --ref 198e803          # by commit SHA
sudo nora upgrade --ref origin/main      # by branch
```

Useful flags:

| Flag | Effect |
|---|---|
| `--ref REF` | Target git ref (default `origin/main`). Accepts tags, branches, and full commit SHAs. |
| `--dry-run` | Print every phase without mutating the filesystem or restarting services. |
| `--check-only` | Pre-flight only — verify the ref resolves and exit. |
| `--no-backup` | Skip the pre-upgrade backup. Operator manages snapshots externally. |
| `--no-restart` | Don't restart `nora-mcp` after upgrade. Useful for offline validation. |
| `--json` | Emit machine-readable JSON to stdout (suitable for CI / Ansible). |
| `--prefix PATH` | Install prefix (default `/opt/nora`). |
| `--config-dir PATH` | Runtime config dir (default `/etc/nora`). |
| `--state-dir PATH` | Mutable state dir (default `/var/lib/nora`). |

Phases (run in order; rollback fires on any failure):

1. **Pre-flight** — `git rev-parse --verify origin/<ref>^{commit}` resolves the target SHA; no-op if already at target.
2. **Backup** — `/etc/nora/{nora.env,nora-mcp.env,signing_key}` + `<prefix>/data/{devices.yaml,oid-catalogs/}` copied to `/var/lib/nora/upgrades/<UTC-timestamp>/` (root 0700). A `MANIFEST.txt` records the pre-upgrade SHA + ref + timestamp.
3. **`git fetch origin`** + `git checkout <target-sha>` (detached HEAD) inside `<prefix>`.
4. **`uv sync`** to refresh dependencies.
5. **Re-sign catalogs** under `<prefix>/data/oid-catalogs/` so the HMAC envelope matches the new tree.
6. **`systemctl restart nora-mcp`** (skippable via `--no-restart`).
7. **Smoke test** — `scripts/verify-install.sh --json --strict` against the upgraded install. Any FAIL or WARN aborts and triggers rollback.

Rollback restores the backup, restarts `nora-mcp`, and exits non-zero with a stderr line naming the failing phase + backup path.

**Manual fallback** (kept for air-gapped hosts, custom packagers, and recovery from a wedged `nora upgrade`):

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

## NORA ↔ OpenChat probe-results contract (issue #61 / PR3)

The probe-results flow mirrors the intervention-memory flow:

```
OpenChat reader          ←       shared directory         ←       NORA writer
(display results / read                  NORA_PROBE_RESULTS_DIR              (PR2 WU-2.4 hook +
 pdf attachments)                          (PRB-*.json + *.pdf)                PR3 WU-3.2 render)
```

When a probe completes (PR2's `_attach_post_completion_result` hook),
NORA atomically writes `<nora_probe_results_dir>/PRB-<sector>-<ap_ip>-<unix>-<6hex>.json`
and a corresponding `.pdf`. OpenChat polls / syncs the directory on
its own schedule. Latency < durability: a write that survives a host
crash is more important than one that propagates in <1 second.

Retention: the PR2 writer does NOT auto-delete old `PRB-*.json`
files. Operators run a `find $NORA_PROBE_RESULTS_DIR -name 'PRB-*.json' -mtime +30 -delete`
cron job (or equivalent) for retention.

Three sharing options: NFS, S3-fuse, periodic rsync — same as
interventions. See "Integration contract: NORA ↔ OpenChat" above
for the deployment guide.

## Troubleshooting

| Symptom (stderr)                                                                    | Likely cause                                                                | Fix                                                                                                  |
|--------------------------------------------------------------------------------------|-----------------------------------------------------------------------------|------------------------------------------------------------------------------------------------------|
| `nora upgrade` failed mid-flight (exit 1 with `phase=<name>` on stderr)              | NORA restored the backup automatically. Inspect the failing phase's stderr in `/var/log/nora/` and check `/var/lib/nora/upgrades/<timestamp>/MANIFEST.txt` for the SHA/ref that was active before the upgrade. | Re-run `sudo nora upgrade --ref <known-good-sha>` to land on the previous version. |
| `CatalogVerificationError: missing signing key (NORA_OID_CATALOG_SIGNING_KEY is empty)` | `.env` not loaded, or key variable is empty                                | Confirm `/etc/nora/nora.env` contains the line; reload systemd: `systemctl daemon-reload && systemctl restart nora-mcp` |
| `CatalogVerificationError: ... HMAC-SHA256 signature mismatch`                        | Key changed; catalog not re-signed, OR catalog was tampered                | Re-sign: `sudo -u nora -E .venv/bin/python scripts/sign_catalog.py`. If `git diff data/oid-catalogs/` shows unexpected changes, the file was modified — restore from a known-good source. |
| `CatalogVerificationError: ... missing required OID(s): modulationMode, ...`         | Catalog file is the wrong shape (e.g. empty, or from a different vendor) | Re-run `sign_catalog.py`; if the script does not regenerate the file, the upstream OID_CATALOG_V1 in the script was edited — pull a fresh `scripts/sign_catalog.py` from the repo. |
| `FileNotFoundError: data/devices.yaml`                                                | `NORA_DEVICES_INVENTORY_PATH` points at a file that does not exist         | Confirm the path; the directory of the path must be readable by the `nora` user.                      |
| `ValidationError: ... vendor Field required`                                          | `devices.yaml` is missing required fields                                   | See INSTALL.md § 1 for the device schema (`vendor`, `model`, `firmware`, `host`, `snmp_version`, `community` or v3 credentials). |
| `intervention_memory: keyword search cap reached`                                    | Operator ran a broad keyword query against a large interventions dir       | Either narrow the query, raise `NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS`, or partition the directory by date. |
| `correlate_sector_interference` returns zero conflicts for a tower you know has carriers | OpenChat POST_MIGRATION records are missing `carrier_frequency_mhz`     | Re-run OpenChat's `deploy_v7_intervention_memory.py` against the production `webui.db` so the new shape overwrites old records. |
| systemd unit fails with `status=203/EXEC`                                             | The venv path in `ExecStart=` is wrong                                     | `ls -l /opt/nora/.venv/bin/nora-mcp`; if missing, re-run `uv sync`.                                 |
| `DeprecationWarning: python -m nora is deprecated`                                    | Something invoked the legacy alias                                         | Use `nora-mcp` instead. The alias is kept only for backward compatibility.                            |
| `probes.persist.ok: zero samples after 60s` | `net.ipv4.ping_group_range` does not cover the `nora` gid. Verify with `sysctl net.ipv4.ping_group_range`. See INSTALL.md "Unprivileged ICMP". |
| `icmp_run_sector_stability_probe → samples_count: 0` | Same root cause as above. The daemon loop exits with all samples received=False because the kernel rejected every sendto on the unprivileged ICMP datagram socket. |

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

## Spectrum sweep — post-sweep HTTP fetch ladder (issue #70 / WU-2)

After the real Cambium sweep completes (`.221.0 == 4` — `idleCompleteSpectrumAnalysis`),
the helper runs a post-sweep HTTP ladder against each radio's web root to
recover the per-bin RF telemetry. This is the third slice of issue #62
(part 1 = sweep protocol in PR #66; part 2 = multi-community in PR #67;
part 3 = HTTP decode in this slice).

**Ladder sequence (only on `final_status == 4`):**

1. `GET http://{host}/SpectrumAnalysis.xml` with bounded retry / timeout
   (`nora_spectrum_http_max_retries=5` × `nora_spectrum_http_retry_delay_seconds=3.0`
   = 15s patience before failing; matches operator's "5 reintentos con
   3s de delay" baseline).
2. If a future tool-wiring slice passes `sm_hosts` to the helper, sleep
   `nora_spectrum_sm_reassociation_timeout_seconds=15.0` (the SMs need
   to re-associate after the coordinated sweep), then sequentially `GET`
   each SM's XML.
3. Parse every payload (stdlib `xml.etree.ElementTree`); aggregate bins
   across AP + SMs.
4. Compute `noise_floor_per_channel` (per-channel worst-leg `avg_dbm`)
   + `rank_clean_frequencies` (top-`nora_spectrum_ranking_top_n`
   worst-case-min-first ordering in MHz).

**Failure policy is non-fatal:** any HTTP fetch failure or XML parse
error is captured in `SpectrumSweepResult.post_sweep_error` (one-line
diagnostic); `ranked_clean_frequencies` + `noise_floor_dbm` stay
empty, and the sweep outcome remains `COMPLETED`. The operator sees
that the sweep ran fine but the bin decode could not be performed.

**Operational timing on physical PMP 450i (firmware 25.0.1, operator-observed):**

| Phase | Typical duration |
|-------|------------------|
| SNMP sweep (AP, sector-coordinated) | ~95-105s |
| SM re-association (after AP sweep) | ~8-15s |
| Per-radio HTTP fetch (with retry on slow web server) | ~5-15s |
| **Total end-to-end (single-AP + N SMs)** | **~120s + N × 10s** |

**New Settings knobs (defaults match operator's production baseline):**

| Knob | Default | Bound | Purpose |
|------|---------|-------|---------|
| `nora_spectrum_http_timeout_seconds` | 10.0 | `[1.0, 60.0]` | Per-attempt HTTP timeout. |
| `nora_spectrum_http_max_retries` | 5 | `[0, 20]` | Retry attempts AFTER the initial GET (0 disables retry). |
| `nora_spectrum_http_retry_delay_seconds` | 3.0 | `[0.1, 30.0]` | Sleep between HTTP attempts. |
| `nora_spectrum_sm_reassociation_timeout_seconds` | 15.0 | `[1.0, 60.0]` | Wait between AP completion and SM XML fetch. |
| `nora_spectrum_ranking_top_n` | 10 | `[1, 100]` | Top-N for `ranked_clean_frequencies`. |

All five are bound by `Settings._validate_spectrum_http_settings` and
fail closed at boot on misconfiguration. Each defaults to the value
the operator uses in production.

**Driver-layer air-gap carve-out:** the post-sweep HTTP fetch is the ONE
exception to the driver-layer air-gap. Cambium radios expose
`SpectrumAnalysis.xml` only over plain HTTP on their web root, and the
spectrum helper MUST reach it. The exception is surgical: the
`httpx` whitelist is keyed by file path RELATIVE TO PROJECT ROOT
(`tests/test_driver_airgap.py::_AIRGAP_EXCEPTIONS`); any new HTTP-using
driver module must be added explicitly. See `tests/test_driver_airgap.py`
for the full enforcement.

**Zero-Leakage:** every host literal in the result is the inventory's
IPv4 literal (already TEST-NET-1 / RFC 5737 sanitised at the inventory
layer); the URL builder does not embed any other identifier. The MCP
tool boundary applies the project-wide `Sanitizer` before serialisation.

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
| 0 | Passive Telemetry (Read-Only) | Direct execution | 8 tools (AP summary, SM table, radio metrics, frame util, SM diagnostics, intervention history search, device lifecycle, sector correlation) + 4 ICMP stability probe tools (`icmp_run_sector_stability_probe`, `icmp_get_sector_stability_progress`, `icmp_cancel_sector_stability_probe`, `icmp_list_probe_runs`) |
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

## PromptOps Workflow (issue #45)

System prompts (`src/nora/prompts/*.md`) are versioned and synchronised
to Open WebUI as immutable, tagged model profiles with a mutable
`latest` alias. The SSoT (Single Source of Truth) is the Git tree;
Open WebUI is a downstream consumer that mirrors the Git state.

### Front-matter contract (system prompts)

Every prompt at `src/nora/prompts/<name>.md` MUST declare front-matter
with:

| Field | Type | Required | Validation |
|-------|------|----------|------------|
| `name` | string | yes | matches filename (sans `.md`) |
| `description` | string | yes | non-empty |
| `version` | string (SemVer) | yes | `packaging.version.Version` must parse |
| `nora_compatibility` | string (SemVer range) | yes | `packaging.specifiers.SpecifierSet`; MUST contain `nora.__version__` |
| `governance` | dict | yes | non-neg ints for `tier_0`, `tier_1`, `tier_2` |
| `checksum_sha256` | 64-char lowercase hex | yes | `sha256(body_bytes).hexdigest()` MUST match the declared value |

`PromptRegistry._validate_system_prompt` enforces this at boot; a
violation raises `PromptNotFoundError` and the prompt is dropped.

### SemVer policy for prompts

| Bump | Meaning | Example |
|------|---------|---------|
| Patch | Clarity, typo fixes, additional few-shot examples | `0.3.5` → `0.3.6` |
| Minor | Tool additions, new governance rules, new operational sections | `0.3.5` → `0.4.0` |
| Major | Structural governance shifts or breaking behavioural modifications | `0.3.5` → `1.0.0` |

The SemVer signal feeds into the NORA version bump: a prompt Minor
warrants a NORA Minor.

### Bumping a prompt

1. Edit the prompt body in `src/nora/prompts/<name>.md`.
2. Update `version` in front-matter.
3. The `checksum_sha256` is computed at scan time — the operator does
   NOT compute it manually. After bumping `version`, run a local boot
   (e.g. `nora mcp` once) to confirm the registry accepts the new
   checksum.
4. Commit with a Conventional Commit message
   (`feat(prompts): ...` / `fix(prompts): ...` / `docs(prompts): ...`).
5. Push. Bump the NORA package version per the SemVer policy above.
6. Run `nora prompt sync` to publish the new immutable profile and
   update the `latest` alias in Open WebUI.

### Manual sync to Open WebUI

```bash
nora prompt sync \
    --base-url "$OPENWEBUI_BASE_URL" \
    --admin-api-key "$OPENWEBUI_ADMIN_API_KEY" \
    --model-base nora-netops \
    --git-sha "$(git rev-parse HEAD)" \
    --release-tag "$(git describe --tags --exact-match 2>/dev/null || echo)"
```

The sync is idempotent — re-running produces the same end state.

Required env vars (override via flags):

| Env var | Default | Purpose |
|---------|---------|---------|
| `OPENWEBUI_BASE_URL` | `http://localhost:8080` | Open WebUI base URL |
| `OPENWEBUI_ADMIN_API_KEY` | (required) | Bearer token for the Open WebUI REST API |

The sync POSTs `<base>-v<X.Y.Z>` (immutable, via `POST /api/v1/models/create` —
treated as success on HTTP 401 with body whose ``detail`` matches
the Open WebUI duplicate-id pattern — either the spec's original
``"Model ID already taken"`` substring or the actual server-constant
text ``"Uh-oh! This model id is already registered..."``
verified against ``constants.py:55``; both substrings are matched)
and POSTs the `<base>-latest` update to `POST /api/v1/models/model/update`
with the alias id carried in the body (mutable alias). Both carry
metadata `{commit_sha, release_tag, synced_at, nora_version, prompt_version}`.

The mutable `<base>-latest` alias is updated via `POST /api/v1/models/model/update`. On a fresh deployment where the alias does not yet exist, Open WebUI returns HTTP 401 with body `{"detail": "We could not find what you're looking for :/"}` (the router at `routers/models.py:785` raises `HTTPException(401, ERROR_MESSAGES.NOT_FOUND)` for missing records). The orchestrator detects this pattern and falls back to a POST on `/create` with the same body — the alias is seeded automatically.

### Instant rollback

When a newly deployed prompt misbehaves against the underlying LLM:

1. Open the Open WebUI model dropdown.
2. Select the prior frozen version tag (`nora-netops:v<prev>`).
3. Done — no SSH, no config edit, no NORA restart.

The mutable `latest` alias continues to point at the broken prompt
until the next successful sync overwrites it. Operators who want to
**pin** a known-good prompt can update Open WebUI's chat-template
default to a specific frozen tag.

### Watermark banner

Every `@mcp.prompt` wrapper and `nora_get_tool_spec` tool returns a
banner prepended to the rendered body:

```
<!-- NORA-PROMPT: <name> v<version> [sha: <first-8-hex>] -->

<body>
```

The banner lets operators ask the LLM "what prompt version are you
running?" and get a deterministic answer from the visible chat
context. The SHA is truncated to 8 hex chars for human readability;
the full 64-char digest is in `Prompt.metadata["checksum_sha256"]`
for drift detection.

### Troubleshooting

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Boot fails with `PromptNotFoundError: <name>: checksum_mismatch` | Body was edited without updating checksum (or without bumping version) | Either revert the body, or recompute the checksum by re-running a local boot — the registry computes and reports the expected value in the error message |
| Boot fails with `PromptNotFoundError: <name>: nora_compatibility_unsatisfied` | Prompt declares a `nora_compatibility` range that doesn't contain the running `nora.__version__` | Either bump NORA to satisfy the range, or relax the prompt's range |
| `nora prompt sync` returns `OpenWebUIAuthError` | Invalid or missing API key | Confirm `OPENWEBUI_ADMIN_API_KEY` is set and has model-create permission |
| `nora prompt sync` returns `OpenWebUISyncError: HTTP transport error` | Open WebUI not reachable at `--base-url` | Confirm `curl <base-url>/api/v1/models -I` returns 200 |
| Open WebUI shows stale prompt after sync | Front-end cached the old model definition | In Open WebUI admin, force-refresh the model entry or restart the Open WebUI container |
