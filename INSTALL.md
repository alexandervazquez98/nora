# Installing NORA MCP

A runnable install of NORA on a single Linux host. Five steps, ~5 minutes.

NORA is a **stdio MCP server**: it speaks JSON-RPC over stdin/stdout to a
client (Open WebUI, Claude Desktop, OpenChat UIWeb, etc.). It does not bind
a TCP port, so there is no firewall or TLS configuration to manage for the
server itself.

## Prerequisites

| Tool     | Minimum version | Notes                                                       |
|----------|-----------------|-------------------------------------------------------------|
| Python   | 3.12+           | NORA uses `match` syntax and PEP 695 generics.              |
| git      | 2.30+           | For cloning the repository.                                 |
| uv       | 0.4+            | Fast Python package manager. Install with `pip install uv`. |
| systemd  | 250+            | For the bundled unit file. Other supervisors work too.      |

Verify before proceeding:

```bash
python3 --version    # must be 3.12 or newer
git --version
uv --version
systemctl --version
```

## 1. Clone and install

```bash
git clone https://github.com/alexandervazquez98/nora.git /opt/nora
cd /opt/nora
uv sync
```

`uv sync` creates `.venv/`, resolves every dependency declared in
`pyproject.toml` (pinned via `uv.lock`), and installs the two console
scripts:

- `nora-mcp` — canonical entry point (use this).
- `nora` — sub-command dispatcher. `nora` (no args) emits a `DeprecationWarning`
  and boots MCP (back-compat alias). `nora mcp` boots MCP. `nora hitl mint …`
  mints a signed HITL approval token.

Verify the install:

```bash
.venv/bin/nora-mcp --help   # will abort on HMAC mismatch; that is expected
```

## 2. Generate the catalog signing key

```bash
.venv/bin/python scripts/generate_signing_key.py
```

Output is a single URL-safe-base64 line, 43 characters, ~256 bits of entropy
from the OS CSPRNG. **Treat this output as a secret** — it is the HMAC-SHA256
key that authenticates your OID catalog at boot. Anyone who holds this key
can sign forged catalogs that NORA will accept as legitimate.

Save the output somewhere only the operator (and the service account) can
read it:

```bash
# Option A — vault (preferred for production)
vault kv put secret/nora/signing_key value="$(.venv/bin/python scripts/generate_signing_key.py)"

# Option B — local file owned by the service user
sudo install -d -m 0750 -o nora -g nora /etc/nora
umask 077
.venv/bin/python scripts/generate_signing_key.py | sudo tee /etc/nora/signing_key >/dev/null
sudo chmod 0600 /etc/nora/signing_key
sudo chown nora:nora /etc/nora/signing_key
```

## 3. Create the runtime environment file

```bash
sudo cp .env.example /etc/nora/nora.env
sudo chmod 0640 /etc/nora/nora.env
sudo chown nora:nora /etc/nora/nora.env
```

Edit `/etc/nora/nora.env` and set:

```ini
NORA_OID_CATALOG_SIGNING_KEY=<paste-the-key-from-step-2>
NORA_OID_CATALOGS_PATH=/opt/nora/data/oid-catalogs/
NORA_DEVICES_INVENTORY_PATH=/opt/nora/data/devices.yaml
NORA_INTERVENTIONS_DIR=/var/lib/nora/interventions/
NORA_INTERVENTIONS_KEYWORD_SEARCH_MAX_RECORDS=1000
NORA_INTERVENTIONS_CORRELATE_SCAN_LIMIT=50
```

`NORA_INTERVENTIONS_DIR` is shared between NORA and OpenChat — both processes
read from and write to (NORA never writes; OpenChat writes) the same
directory. Pick a path both hosts can reach: an NFS mount, an S3-fuse
mount, or any POSIX-shared filesystem.

## 4. Sign the catalog

The OID catalog at `data/oid-catalogs/cambium/pmp450i/15.2.1.json` is
shipped without a valid HMAC (the signing key is environment-specific).
Generate a fresh signature for your environment:

```bash
sudo -u nora -E .venv/bin/python scripts/sign_catalog.py
```

`-E` preserves `NORA_OID_CATALOG_SIGNING_KEY` from the environment. The
script writes a signed envelope in place. Commit the signed catalog if you
manage the catalog as part of your infrastructure config repo; do not
commit the key.

## 5. Install and start the systemd service

```bash
sudo install -d -m 0750 -o nora -g nora /var/log/nora
sudo install -d -m 1777 -o nora -g nora /var/lib/nora/interventions
sudo cp scripts/nora-mcp.service /etc/systemd/system/nora-mcp.service
sudo systemctl daemon-reload
sudo systemctl enable --now nora-mcp
```

Verify:

```bash
sudo systemctl status nora-mcp           # should be "active (running)"
sudo journalctl -u nora-mcp -n 50        # should show "nora-mcp boot complete"
```

## 6. Synchronise prompts to Open WebUI (recommended for chat-front-end deployments)

System prompts are versioned in Git and exported to Open WebUI as
immutable model profiles via the `nora prompt sync` subcommand.
Operators running an Open WebUI front-end (or any other MCP-aware
chat UI that consumes Open WebUI's model registry) SHOULD run sync
once per NORA release to keep the front-end's model dropdown aligned
with the canonical prompts.

### Required environment variables

| Variable | Required | Default | Purpose |
|----------|----------|---------|---------|
| `OPENWEBUI_BASE_URL` | no | `http://localhost:8080` | Open WebUI base URL |
| `OPENWEBUI_ADMIN_API_KEY` | **yes** | (none) | Bearer token with model-create permission |

### Sync command

```bash
export OPENWEBUI_BASE_URL="http://localhost:8080"
export OPENWEBUI_ADMIN_API_KEY="<your-admin-key>"

nora prompt sync \
    --git-sha "$(git rev-parse HEAD)" \
    --release-tag "$(git describe --tags --exact-match 2>/dev/null || true)"
```

### What gets created

- `<model_base>-v<X.Y.Z>` — **immutable** versioned profile (POST to
  `/api/v1/models/create`). Re-running sync with the same NORA version
  is a no-op (server returns HTTP 401 with body
  `{"detail": "Model ID already taken"}`, treated as success).
- `<model_base>-latest` — **mutable alias** updated in place (POST to
  `/api/v1/models/model/update` with id in body) to point at the
  freshly-synced version.

`<model_base>` defaults to `nora-netops` and is configurable via
`--model-base`.

### Instant rollback

In Open WebUI's model dropdown, the prior frozen version tag
(`nora-netops:v0.3.5` for example) is selectable. Switching the
chat session to that tag rolls back the prompt without restarting
NORA or touching the local filesystem. The `latest` alias continues
to point at the latest synced version until the next sync.

### Skip this step if

- You are deploying NORA as a backend for an MCP client that consumes
  `@mcp.prompt` directly (no Open WebUI model registry involved).
- You prefer to maintain Open WebUI Modelfiles manually. The
  zero-leak pass on the canonical prompts in Git is unaffected by
  Open WebUI state.

## Smoke test

Talk to the running server over stdio (the systemd unit uses the same
`ExecStart` as a manual invocation). Replace `<api-key>` with your LLM
provider key if you have one wired in, or omit it for a read-only test:

```bash
sudo -u nora /opt/nora/.venv/bin/nora-mcp <<'JSON'
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"smoke","version":"0"}}}
{"jsonrpc":"2.0","method":"notifications/initialized"}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
JSON
```

Expected output: three JSON-RPC responses, the second enumerating the four
canonical tools:

```
snmp_get_pmp450i_radio_metrics
search_intervention_history
get_device_lifecycle_summary
correlate_sector_interference
```

## Automated install

Steps 1–5 above are reproducible from a single shell script:

```bash
sudo scripts/install.sh
```

The script is idempotent — re-running it on an already-installed host is a
no-op with `[SKIP]` lines for each completed phase. Override the defaults
when needed:

```bash
sudo scripts/install.sh \
    --prefix /opt/nora \
    --config-dir /etc/nora \
    --state-dir /var/lib/nora \
    --log-dir /var/log/nora \
    --user nora
```

Useful flags:

| Flag | Effect |
|---|---|
| `--dry-run` | Print every step without changing the filesystem. |
| `--skip-signing-key` | Don't generate / overwrite `/etc/nora/signing_key`. |
| `--skip-systemd` | Don't install / enable the systemd unit. |
| `--skip-catalog` | Don't re-sign the OID catalogs. |
| `--force-env-file` | Overwrite `/etc/nora/nora.env` from `.env.example`. |
| `--user NAME` | Service-account username (default `nora`). |

Always run `verify-install.sh` afterwards:

```bash
sudo scripts/verify-install.sh
# or, machine-readable for CI:
sudo scripts/verify-install.sh --json --strict
```

`verify-install.sh` checks binary versions, paths, file modes (signing_key
must be `0600`, `nora.env` must be `0640` or `0600`), the systemd unit
state, and — most importantly — spawns `nora-mcp` over stdio and validates
the JSON-RPC handshake plus `tools/list` (4 expected tools) and
`prompts/list` (2 expected prompts after the prompts subsystem landed).
Exit codes: `0` = OK, `1` = at least one FAIL, `2` = `--strict` and any
WARN.

The manual steps above remain valid for operators who need to debug a
specific phase; the script is a thin wrapper over the same procedure and
not a separate code path.

### One-line bootstrap (advanced / IaC only)

> **For advanced operators and IaC pipelines only.** The recommended
> install flow remains the manual `git clone` + `cd` + `sudo scripts/install.sh`
> shown above. The bootstrap script is a wrapper that automates the
> `clone + cd + install` sequence for cases where piping from `curl`
> saves real work (cloud-init `runcmd`, Ansible `command=`, Terraform
> `local-exec`). Operators who can run three commands do not need this.

```bash
curl -fsSL https://raw.githubusercontent.com/alexandervazquez98/nora/main/scripts/bootstrap.sh | sudo bash -s --
```

Pin a ref:

```bash
curl -fsSL https://raw.githubusercontent.com/alexandervazquez98/nora/main/scripts/bootstrap.sh | \
    sudo bash -s -- --ref v0.2.0
```

Point at an internal mirror (air-gapped):

```bash
curl -fsSL https://internal-mirror.example.com/nora/scripts/bootstrap.sh | \
    sudo bash -s -- --repo https://internal-mirror.example.com/nora.git
```

Download only — clone for audit without installing:

```bash
curl -fsSL https://raw.githubusercontent.com/alexandervazquez98/nora/main/scripts/bootstrap.sh | \
    sudo bash -s -- --download-only --dest /tmp/nora-review
```

What `bootstrap.sh` does:

1. Validates that `--repo` is HTTPS (refuses `git://`, `http://`, etc.).
2. Validates that `--ref` contains no shell-injection vectors
   (`--upload-pack=`, `ext::`, `;`, `|`, `$`, backtick, backslash,
   whitespace, or leading `-`).
3. Refuses to run as direct root (empty `SUDO_USER`); require
   `sudo bash -s -- ...` instead.
4. Clones the repo to a deterministic `/tmp/nora-bootstrap-<timestamp>`,
   or to `<dest>/nora` for `--download-only`.
5. If the destination already contains a `.git`, runs `git pull --ff-only`
   instead of cloning (idempotent).
6. Captures the cloned commit SHA, then invokes `scripts/install.sh`
   with `--prefix` and `--config-dir` forwarded.
7. After install, verifies the installed tree's HEAD matches the cloned
   SHA when `/opt/nora/.git` exists.
8. Cleans up `/tmp/nora-bootstrap-*` on install success unless
   `--keep-clone` was passed. On install failure, the clone is
   preserved and the failure message names the path.

Available flags: `--ref`, `--repo`, `--prefix`, `--config-dir`,
`--download-only`, `--dest`, `--keep-clone`, `--yes`, `--help`.
Run `bash scripts/bootstrap.sh --help` for the full reference.

## What is NOT covered here

- **OpenChat integration**: NORA and OpenChat share
  `NORA_INTERVENTIONS_DIR`. Both must agree on the same path; OpenChat
  writes JSON, NORA reads JSON. See OPERATIONS.md for the read/write
  contract and `tests/intervention_memory/test_openchat_writer_contract.py`
  for the pinned regression.
- **TLS / network exposure**: NORA defaults to stdio. To enable
  HTTP/SSE: edit `/etc/nora/nora-mcp.env` (`NORA_MCP_TRANSPORT=http`,
  `_HOST=127.0.0.1`, `_PORT=8005`), then `sudo systemctl restart
  nora-mcp` (no `daemon-reload` needed for env-only changes). If your
  MCP client is remote, terminate TLS at a reverse proxy in front of
  the localhost bind — never expose NORA directly.
- **HA / multi-replica**: NORA has no internal coordination state. You can
  run multiple replicas reading the same `NORA_INTERVENTIONS_DIR` (the
  read path is stateless and the boot guard rejects tampered catalogs
  equally on every replica).

## Troubleshooting

See `OPERATIONS.md` § "Troubleshooting" for the error-by-error table.

## Unprivileged ICMP (issue #61 / PR3 prerequisite)

The Cambium PMP 450i sector stability probe uses **unprivileged
ICMP** — it sends and receives ICMP echo packets on a datagram
socket (`IPPROTO_ICMP` via `socket.SOCK_DGRAM`), which on Linux only
requires the sender's gid to be in `net.ipv4.ping_group_range`. It
does NOT require `CAP_NET_RAW` and does NOT change the systemd
hardening (`PrivateDevices=true`, `NoNewPrivileges=true`) shipped
with NORA.

### sysctl wire-up

Set the host's `ping_group_range` to cover the `nora` group:

    sudo sysctl -w net.ipv4.ping_group_range=0 2147483647

(Production deployments can scope this tighter: e.g.
`sudo sysctl -w net.ipv4.ping_group_range=<nora_gid> 2147483647`.)

To make this survive reboots, persist it:

- Debian/Ubuntu: write to `/etc/sysctl.d/99-nora.conf`:

    net.ipv4.ping_group_range = 0 2147483647

- RHEL-family: write to `/etc/sysctl.d/99-nora.conf` (same body).

Then `sudo sysctl --system`.

### Verify

After the sysctl wire-up and a `systemctl restart nora-mcp`, the
probe should produce samples. If `icmp_get_sector_stability_progress`
returns `samples_count == 0` after the daemon has been running for
several intervals, the sysctl is almost certainly the cause — see
the troubleshooting recipe in OPERATIONS.md.

### No raw-socket alternatives

If the sysctl cannot be set on the deployment host (e.g. a hard
CIS-benchmark block), PR4 will land a `subprocess` fallback that
shells out to a setcap'd `/bin/ping`. PR3 does NOT include this
fallback.
