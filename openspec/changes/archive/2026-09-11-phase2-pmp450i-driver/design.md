# Design: PMP 450i SNMP Driver (Phase 2 — Driver Layer)

## Technical Approach

This change ships the first read-only SNMP driver for Cambium PMP 450i on top of Phase 1's FastMCP server, additive `Settings`, R10-redacted auto-trace, and `_sanitizer`. Three capabilities layer: **prompt-registry** loads Markdown prompts once at boot (`nora/prompts/registry.py`); **oid-catalog** verifies per-firmware JSON files via HMAC-SHA256 against `Settings.oid_catalog_signing_key` (`nora/drivers/oid_catalog.py`); **driver-snmp-pmp450i** is the single `@mcp.tool` calling `nora_session_set_focus(device_id)` first, resolving the catalog for `device.firmware`, fanning out `puresnmp.Client.get(oid)` per OID, and folding into a Pydantic `RadioMetricsReport`.

`__main__.py` boots `PromptRegistry` → `OidCatalogRegistry.verify_all(settings)` → injects both into the driver facade. Both are boot-fatal on failure. Writes are blocked: `RefusesWriteError` from a public-API guard, plus an AST lint rejecting `set|update|write|setbulk|bulk_set` under `src/nora/drivers/`. Wire traffic goes through one `puresnmp` facade (`v2c.py` / `v3.py`); tests run against `snmpsim` (`@pytest.mark.slow`) plus pure-unit folding tests.

## Architecture Decisions

### Decision: Driver boundary — per-vendor sub-package

| Option | Tradeoff | Decision |
|---|---|---|
| `src/nora/drivers/snmp_pmp450i/{client,v2c,v3,report,driver,exceptions}.py` | Adds nesting; isolates Cambium specifics behind one folder so Phase 3 (PTP/PMP-switch/SSH) lands as siblings without churn. | **Chosen** |
| Flat `src/nora/drivers/*.py` | Fewer files now; will refactor when driver #2 lands. | Rejected (rules.design: "Keep drivers behind interfaces; no vendor detail leaks into the core") |
| `src/nora/driver/{cambium,...}` | Confuses the abstraction layer. | Rejected |

**Rationale**: matches rules.design ("Keep drivers behind interfaces"). Per-vendor folder is the seam for Phase 3 siblings. (Driver-R1, OidCatalog-R1)

### Decision: Client facade — thin Protocol, not ABC

| Option | Tradeoff | Decision |
|---|---|---|
| `typing.Protocol` (`get_oid`, `walk`, `close`) on `SnmpClient`, concrete `V2CClient` / `V3Client` | Structural typing; no inheritance cost; `mypy --strict` happy without explicit ABC. | **Chosen** |
| `abc.ABC` base class | Forces inheritance, harder to fake in property tests. | Rejected |
| Module-level `get_oid()` function | Loses the per-call client lifetime needed for `puresnmp`'s async API. | Rejected |

**Rationale**: structural typing, `mypy --strict` happy, property test trivial via `mock.create_autospec`. `puresnmp` import isolated to `v2c.py`/`v3.py`. (Driver-R1, R2)

### Decision: OID resolution — pydantic-validated JSON

| Option | Tradeoff | Decision |
|---|---|---|
| JSON file → `OidCatalog(BaseModel)` keyed by stable object name, with `REQUIRED_OIDS: frozenset[str]` enforced at boot | Pydantic gives missing-key error formatting; matches existing `session_models.py` pattern; runs once at boot. | **Chosen** |
| Raw `dict[str, str]` | One line shorter; loses schema error. | Rejected |
| YAML catalog | New dep, no benefit. | Rejected (explore.md decision 4) |

**Rationale**: missing-key context surfaces in the typed exception; matches `session_models.py`; one `REQUIRED_OIDS` constant avoids fold/validate drift. (OidCatalog-R1, R5)

### Decision: HMAC verification — at module boot

| Option | Tradeoff | Decision |
|---|---|---|
| Verify each catalog file once when the registry is constructed in `__main__` | Single sweep; fail-fast on tamper; matches "verify all catalog files" wording. | **Chosen** |
| Verify on every `lookup(name)` call | Wastes CPU and contradicts "exactly once at boot" tone. | Rejected |
| Lazy verify on first `lookup` | Still boots if first device fails — masks operator error. | Rejected |

**Rationale**: single sweep, fail-fast on tamper. Re-signing produces expected `CatalogVerificationError` then a passing boot. (OidCatalog-R3, R4)

### Decision: Prompt loading — synchronous one-shot at boot

| Option | Tradeoff | Decision |
|---|---|---|
| `PromptRegistry.scan(prompts_dir)` at boot; immutable after | Matches the "one-shot boot load" + "missing prompt at boot is fatal" pair; deterministic for tests. | **Chosen** |
| Lazy `get(name)` with on-disk read + cache | I/O on first lookup; ambiguous ownership of "no I/O on second call" claim. | Rejected |
| inotify / watchdog | Forbidden by Prompt-R1. | Rejected |

**Rationale**: validates front-matter once; typed `PromptNotFoundError`. Package-data fallback is an explicit `Path`, not `os.environ`. (Prompt-R1..R5)

### Decision: Air-gap enforcement — AST scan built into the test runner

| Option | Tradeoff | Decision |
|---|---|---|
| Mirror `tests/test_session_journal_airgap.py` AST scan over `src/nora/drivers/` + `src/nora/prompts/` as a fast test | Same shape as the Phase 2 SessionJournal gate; CI runs it on every commit; pre-commit is a redundant copy. | **Chosen** |
| Pre-commit hook only | Bypasses CI if a contributor skips hooks. | Rejected |
| Both pre-commit + test | Duplicate maintenance, two failure surfaces. | Rejected |

**Rationale**: CI runs it on every commit; pre-commit is redundant. Mirrors session-journal R8 with the spec's banned-import list. (Driver-R7, OidCatalog-R7)

### Decision: Read-only enforcement — two layers

| Option | Tradeoff | Decision |
|---|---|---|
| Public-API guard (`SnmpClient` Protocol only exposes `get`/`walk`) **plus** AST lint that fails `ruff` on write identifiers under `src/nora/drivers/` | Defense in depth; the lint catches a future `.set()` someone adds before tests even run. | **Chosen** |
| Property test only | Runs at test time only. | Rejected |
| Import-time guard | Brittle, easy to bypass with `puresnmp.Client` directly. | Rejected |

**Rationale**: defense in depth — the lint catches a future `.set()` before tests run. `RefusesWriteError` is the typed exception for any defensive helper. (Driver-R2)

## Data Flow

    MCP client ──► @mcp.tool snmp_get_pmp450i_radio_metrics(device_id)
                       │
                       ├─► nora_session_set_focus(device_id)   ──► SessionJournal
                       │
                       ├─► inventory.get(device_id)             ──► DeviceNotFoundError?
                       │     └─► .oid_catalog_ref (vendor, model, firmware)
                       │
                       ├─► oid_registry.resolve(ref)            ──► CatalogNotFoundError?
                       │     └─► OidCatalog (REQUIRED_OIDS ⊆ keys?)
                       │
                       ├─► puresnmp Client (v2c | v3 + Auth + Priv)
                       │     ├─ .get(oid) × N (within-call cache, Driver-R6)
                       │     └─ Timeout / NoSuchOID / FaultySNMPImplementation
                       │           └─► SnmpTimeoutError / NetworkUnreachableError
                       │
                       ├─► ReportFolding.fold(values, catalog)   ──► RadioMetricsReport
                       │
                       ├─► return Report (typed — opts out of Sanitizer)
                       │     └─► free-text errors → _sanitizer.sanitize(str(exc)).text
                       │
                       └─► auto-trace middleware (R3) records one SessionStep

## File Changes

| File | Action | Description |
|---|---|---|
| `pyproject.toml` | Modify | Add `puresnmp==2.0.1`, `puresnmp-crypto` to `[project.dependencies]`; `snmpsim` to `[dependency-groups].dev`. No `pytest-asyncio`; no `asyncio_mode`. |
| `.env.example` | Modify | Add `NORA_OID_CATALOGS_PATH`, `NORA_DEVICES_INVENTORY_PATH`, `NORA_OID_CATALOG_SIGNING_KEY`, `NORA_PROMPTS_DIR` (RFC 5737 + `change-me` placeholders only). |
| `src/nora/config.py` | Modify | Extend `Settings` with `oid_catalogs_path: Path`, `devices_inventory_path: Path`, `oid_catalog_signing_key: SecretStr`, `prompts_dir: Path | None`. `extra="ignore"` keeps backward compat. |
| `src/nora/__main__.py` | Modify | After `Settings()` and `init_session_journal(...)`: build `PromptRegistry`, `OidCatalogRegistry.verify_all(...)`, then `set_runtime_state(settings, provider, prompt_registry, oid_registry)`. |
| `src/nora/server.py` | Modify | Import `set_focus` already wired; add `@mcp.tool snmp_get_pmp450i_radio_metrics(device_id) -> dict[str, Any]` (Pydantic `model_dump(mode="json")` for serialisation). Reuse `_sanitizer` for error strings only. |
| `src/nora/drivers/__init__.py` | Create | Empty marker; re-export `RadioMetricsReport`, `Device`, `OidCatalog`, exception hierarchy. |
| `src/nora/drivers/exceptions.py` | Create | `DriverError`, `RefusesWriteError`, `DeviceNotFoundError`, `NetworkUnreachableError`, `SnmpTimeoutError`, `CatalogNotFoundError`, `CatalogVerificationError`, `PromptNotFoundError`. One shared module per task contract. |
| `src/nora/drivers/inventory.py` | Create | `Device` Pydantic model + `Inventory` loader (YAML, `pydantic-settings` compatible). |
| `src/nora/drivers/oid_catalog.py` | Create | `OidCatalog(BaseModel)`, `OidCatalogRegistry.verify_all(settings)`. HMAC-SHA256 via `hmac.compare_digest`. `REQUIRED_OIDS` frozenset. |
| `src/nora/drivers/registry.py` | Create | Module-level `DriverRegistry` singleton + boot injection seam. |
| `src/nora/drivers/snmp_pmp450i/__init__.py` | Create | Re-export the facade. |
| `src/nora/drivers/snmp_pmp450i/client.py` | Create | `SnmpClient` `typing.Protocol` (`get_oid`, `walk`, `close`). |
| `src/nora/drivers/snmp_pmp450i/v2c.py` | Create | `V2CClient` wrapping `puresnmp.Client(host, V2C(community), port, timeout)`. |
| `src/nora/drivers/snmp_pmp450i/v3.py` | Create | `V3Client` wrapping `puresnmp.Client(host, V3(user, Auth, Priv), port, timeout)`. |
| `src/nora/drivers/snmp_pmp450i/report.py` | Create | `RadioMetricsReport` Pydantic; `REQUIRED_OIDS`; `fold(values, catalog)`. |
| `src/nora/drivers/snmp_pmp450i/driver.py` | Create | `Pmp450iDriver.fetch_radio_metrics(device_id) -> RadioMetricsReport` — the only public surface; calls `set_focus` first. |
| `src/nora/prompts/__init__.py` | Create | Empty marker; re-export `PromptRegistry`, `Prompt`. |
| `src/nora/prompts/registry.py` | Create | `Prompt(BaseModel)`, `PromptRegistry.scan(source_dir)`, `get(name)`. Front-matter parsed with `yaml.safe_load`. `PromptNotFoundError` raised on missing front-matter / empty `description` / unknown name. |
| `src/nora/prompts/snmp_pmp450i.md` | Create | Shipped system prompt: declares `snmp_get_pmp450i_radio_metrics` tool name and `RadioMetricsReport` schema; zero-leakage rules. No private IPs, MACs, serials, or hostnames. |
| `data/oid-catalogs/cambium/pmp450i/15.2.1.json` | Create | v1 catalog fixture (public-name → dotted-OID; ≤ 20 OIDs; no MIB prose). |
| `data/devices.example.yaml` | Create | Public example inventory (RFC 5737 host, `change-me` credentials). |
| `tests/conftest.py` | Modify | Add `hermetic_settings`/`tmp_prompts_dir`/`tmp_catalogs_dir`/`sample_catalog`/`sample_inventory` fixtures. |
| `tests/test_driver_snmp_pmp450i.py` | Create | Unit + property tests for Driver-R1…R7. |
| `tests/test_oid_catalog.py` | Create | Catalog-R1…R7 unit tests. |
| `tests/test_inventory.py` | Create | Inventory loader tests. |
| `tests/test_prompts.py` | Create | Prompt-R1…R7 unit tests. |
| `tests/test_driver_airgap.py` | Create | Mirrors `test_session_journal_airgap.py` over `src/nora/drivers/` + `src/nora/prompts/`. |
| `tests/test_driver_snmpsim_v2c.py` | Create | `@pytest.mark.slow` integration against `snmpsim`. |
| `tests/test_driver_snmpsim_v3.py` | Create | `@pytest.mark.slow` integration against `snmpsim` (auth+priv). |
| `tests/test_server.py` | Modify | New auto-trace case for `snmp_get_pmp450i_radio_metrics`; typed-return + sanitizer-boundary contract. |

## Interfaces / Contracts

```python
# src/nora/drivers/inventory.py
class SnmpVersion(Literal["v2c", "v3"]): ...

class Device(BaseModel):
    device_id: str
    vendor: Literal["cambium"]
    model: Literal["pmp450i"]
    firmware: str  # e.g. "15.2.1" — pinned to a catalog
    host: str      # RFC 5737 placeholder in fixtures; never a real IP in examples
    port: int = 161
    snmp_version: SnmpVersion
    community: SecretStr | None = None       # v2c only
    auth_password: SecretStr | None = None   # v3 only
    priv_password: SecretStr | None = None   # v3 only

class Inventory:
    @classmethod
    def from_yaml(cls, path: Path) -> "Inventory": ...
    def get(self, device_id: str) -> Device: ...   # raises DeviceNotFoundError

# src/nora/drivers/oid_catalog.py
REQUIRED_OIDS: Final[frozenset[str]] = frozenset({
    "radioDownlinkRate", "radioUplinkRate",
    "signalStrengthRx", "signalStrengthTx",
    "ssr", "modulationMode",
    # …public object names only — see Open Questions
})

class OidCatalog(BaseModel):
    vendor: str
    model: str
    firmware: str
    oids: dict[str, str]  # object_name -> dotted_oid

class OidCatalogRegistry:
    @classmethod
    def verify_all(cls, settings: Settings) -> "OidCatalogRegistry": ...
    def resolve(self, ref: tuple[str, str, str]) -> OidCatalog: ...

# src/nora/drivers/snmp_pmp450i/report.py
class RadioMetricsReport(BaseModel):
    device_id: str
    fetched_at: datetime
    firmware: str
    radio_dl_rate_bps: int
    radio_ul_rate_bps: int
    rx_signal_dbm: int
    tx_signal_dbm: int
    ssr: int
    modulation: str
    # No `dict`/`Any` fields. (Driver-R3)
    @classmethod
    def fold(cls, device: Device, catalog: OidCatalog, values: dict[str, str]) -> "RadioMetricsReport": ...

# src/nora/prompts/registry.py
class Prompt(BaseModel):
    name: str
    description: str
    body: str

class PromptRegistry:
    @classmethod
    def scan(cls, source: Path) -> "PromptRegistry": ...
    def get(self, name: str) -> Prompt: ...   # raises PromptNotFoundError

# src/nora/server.py — single new tool
@mcp.tool
def snmp_get_pmp450i_radio_metrics(device_id: str) -> dict[str, Any]:
    """Fetch a typed RadioMetricsReport for the named device.
    Refuses any write op; sets session focus first; auto-traced."""
```

## Testing Strategy

Strict TDD per `openspec/config.yaml` rules.apply.tdd. RED → GREEN → REFACTOR per module.

| Layer | What | Approach | Maps to spec |
|---|---|---|---|
| Unit | PromptRegistry scan/get + front-matter | `tests/test_prompts.py` | Prompt-R1, R2, R3, R4, R5, R6 |
| Unit | OidCatalog HMAC + missing-key | `tests/test_oid_catalog.py` | OidCatalog-R1…R6 |
| Unit | Device + Inventory YAML | `tests/test_inventory.py` | Driver-R1, R5 |
| Unit | RadioMetricsReport.fold (typed contract) | `tests/test_driver_snmp_pmp450i.py` | Driver-R1, R3 |
| Unit | Protocol guard (no write verbs exposed) + AST lint test | `tests/test_driver_snmp_pmp450i.py` + ruff grep test | Driver-R2 |
| Unit | Static + runtime air-gap scan | `tests/test_driver_airgap.py` | Driver-R7, OidCatalog-R7 |
| Unit | Free-text error sanitised, typed opts out | `tests/test_server.py` | Driver-R3 |
| Property | Hypothesis over `SnmpClient` exported methods → empty write set | `tests/test_driver_snmp_pmp450i.py` | Driver-R2 |
| Property | Hypothesis: any `Device` with R10-named field produces redacted auto-trace entry | `tests/test_driver_snmp_pmp450i.py` | Driver-R3, R7 |
| Integration | `@pytest.mark.slow` — `snmpsim` v2c full fetch | `tests/test_driver_snmpsim_v2c.py` | Driver-R1, R6 |
| Integration | `@pytest.mark.slow` — `snmpsim` v3 auth+priv full fetch | `tests/test_driver_snmpsim_v3.py` | Driver-R1 |

No `pytest-asyncio`. Each async helper uses `asyncio.run(...)` exactly like `tests/test_server_session_tools.py:83-84`. `snmpsim` boots as a subprocess; `subprocess` import lives in test files only (outside `src/nora/drivers/`). Hypothesis is pinned (`>=6`) — no profile block needed unless suites grow slow; mark with `@pytest.mark.slow` for any 100ms+ example.

## Threat Matrix

This change is **pure-Python I/O against local files** and an async SNMP client. It does not change routing, shell, VCS, PR automation, executable-file classification, or process integration. Per `references/threat-matrix.md` rules, the matrix below is recorded as **N/A across the board** with the reason explicit; no manufactured tasks.

| Boundary | Applicability | Reason |
|---|---|---|
| Documentation-like paths | N/A | Driver modules are not `requirements.txt`, `CMakeLists.txt`, `*.md` Markdown that runs code, or `README.sh`. |
| Git repository selection | N/A | No `git -C`/relative/absolute path branching introduced. |
| Commit state | N/A | Driver layer does not stage or commit. |
| Push state | N/A | No VCS write introduced. |
| PR commands | N/A | Tooling lives in repo, not in driver layer. |

The only I/O boundary the driver adds is **catalog file reads at boot** (OidCatalog-R1, R2). Risk model: a tampered catalog file is rejected by HMAC and the process fails closed. The corresponding RED test is `test_tampered_catalog_raises_catalog_verification_error` in `tests/test_oid_catalog.py` — already covered by Unit-layer contract above; no additional design task required.

## Migration / Rollout

No migration required. The change is purely additive: three new `Settings` fields, a new package (`drivers/`, `prompts/`), and one new `@mcp.tool`. Existing `Settings` callers are shielded by `extra="ignore"` (`src/nora/config.py:49`). Rollback is a file deletion per `proposal.md` "Rollback Plan". Phase 1 toolchain + Phase 2 SessionJournal are untouched.

## Open Questions

- [ ] **v1 catalog OID list (15–20 entries)** — `data/oid-catalogs/cambium/pmp450i/15.2.1.json` needs a public-name-only object list sufficient for the fold. Source from Cambium public docs / community forums, never vendored WHISP-SM-MIB. Resolve in `sdd-tasks`.
- [ ] **`_sample_catalog` fixture path** — `tests/conftest.py` needs a tmp-path builder: JSON under `tmp_catalogs_dir/cambium/pmp450i/15.2.1.json`, signed with a deterministic test key. Naming TBD.
- [ ] **`puresnmp` exception mapping table** — `src/nora/drivers/exceptions.py` mirrors `src/nora/llm.py:124-138`'s `_SDK_ERROR_MAP`. Map `puresnmp.exc.{SnmpError, NoSuchOID, Timeout, FaultySNMPImplementation, TooBig, BadValue, ReadOnly, NoAccess}` to `{DriverError, DeviceNotFoundError, SnmpTimeoutError, NetworkUnreachableError, …}`. Confirm in `sdd-tasks`.
- [ ] **`PyWrapper` vs raw `Client`** — `PyWrapper` returns native types (`str`, `int`) folding cleanly into the Pydantic model; raw `Client` returns `x690` types needing extra parsing. Recommend `PyWrapper`; some library examples default to raw.