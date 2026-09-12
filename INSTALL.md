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
- `nora` — deprecated alias for `python -m nora`; emits a `DeprecationWarning`
  and delegates to `nora-mcp`. Kept for backward compatibility only.

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

## What is NOT covered here

- **OpenChat integration**: NORA and OpenChat share
  `NORA_INTERVENTIONS_DIR`. Both must agree on the same path; OpenChat
  writes JSON, NORA reads JSON. See OPERATIONS.md for the read/write
  contract and `tests/intervention_memory/test_openchat_writer_contract.py`
  for the pinned regression.
- **TLS / network exposure**: NORA is stdio-only. If your MCP client is
  remote, tunnel stdio over SSH or use a streamable-HTTP proxy that you
  secure yourself.
- **HA / multi-replica**: NORA has no internal coordination state. You can
  run multiple replicas reading the same `NORA_INTERVENTIONS_DIR` (the
  read path is stateless and the boot guard rejects tampered catalogs
  equally on every replica).

## Troubleshooting

See `OPERATIONS.md` § "Troubleshooting" for the error-by-error table.
