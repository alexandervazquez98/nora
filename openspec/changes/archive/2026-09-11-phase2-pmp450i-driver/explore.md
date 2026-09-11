## Exploration: PMP 450i SNMP Driver

### Current State

The project is on `main` with two landed PRs: `22d3830` (Phase 1 toolchain foundation) and `2e86bba` (SessionJournal — auto-trace middleware + 4 explicit recall tools, PR #5). The Phase 2 sibling change is archived at `openspec/changes/archive/2026-09-10-phase2-session-journal/`; `openspec/specs/session-journal/spec.md` is the canonical source of truth.

**Code layout today** (`src/nora/`): `__init__.py`, `__main__.py`, `config.py`, `llm.py`, `sanitizer.py`, `server.py`, plus `core/{session_journal,session_models,session_paths,session_redaction,session_rotation}.py`. **No `drivers/` package exists.** No `prompts/` package exists. No `data/` directory exists. No `puresnmp` or `snmpsim` in `pyproject.toml` or `uv.lock`.

**Auto-trace already records every `@mcp.tool` call** (`src/nora/server.py:228-282` `_AutoTraceMiddleware`). The R10 redaction list (`src/nora/core/session_redaction.py:19-33`) already covers `community`, `community_string`, `auth_password`, `auth_key`, `priv_password`, `priv_key` — SNMP credentials added to any driver-bound tool call will be auto-redacted before persistence; no work needed in this change for that contract.

**`nora_session_set_focus(device_id)` already exists** at `src/nora/server.py:175-183` (registered as an `@mcp.tool`, returns serialised `SessionState`). The cross-session-journal spec mandates the PMP450i driver call this tool: `openspec/specs/session-journal/spec.md` cross-ref: `phase2-pmp450i-driver ... MUST call nora_session_set_focus(device_id)`.

**Sanitizer contract** (`src/nora/sanitizer.py:116-192`): free-text strings get masked in four categories (private IPv4/MAC/serial/hostname) with deterministic per-instance aliases; non-strings raise `SanitizerInputError`. The `session-journal/spec.md` R6 confirms structured fields bypass sanitization — by composition, a Pydantic-typed return from a `@mcp.tool` is auto-serialised and never traverses `sanitize()`.

**`LMStudioProvider` path** (`src/nora/llm.py:122-138`) drives tool use via `model.respond(...)`, **not** `model.act(...)`. The proposal's "LLM discovers the tool via `provider.model.act(...)`" line is wrong against the current Phase 1 wiring — the LLM does not yet invoke tools; it just sees them in the tool schema when an MCP client renders the server. Tool-call routing today is client-side, not server-driven.

**Toolchain gates** (`openspec/config.yaml`): coverage threshold **85%**, `pytest-cov` 7.x, `--cov=src/nora --cov-report=term-missing` is the verify command. `ruff` + `mypy --strict src/nora` gates. `make test` → `$(PY) -m pytest`. No `asyncio_mode` set in `[tool.pytest.ini_options]` — `pytest-asyncio` is **not** pinned; tests in `tests/test_server_session_tools.py:83-84` use `asyncio.run(_call_tool(...))` wrappers, and new tests must follow the same pattern. `hypothesis>=6` is pinned but **no `[tool.hypothesis]` block** and **no profile** — slow suites use `@pytest.mark.slow` (e.g. `tests/core/test_session_journal_property.py:21`) and aren't part of the default run.

**Proposal-vs-reality gaps in the existing `proposal.md`**:
1. The "Out of Scope" block says `phase2-session-journal` is "separate change" — that change is now `archive/2026-09-10-phase2-session-journal/`. The wording must be reconciled; the dependency is satisfied, not pending.
2. The proposal names `src/nora/prompts/{snmp_pmp450i,icmp_endpoint,ssh_switch,health}.md` + `registry.py` as inherited from Phase 1. None of these exist on disk or in `uv.lock`. The session-journal archive's memory `obs-c0a0f25700260a4d` ("no LLM prompt changes needed") was scoped to the journal change, not to Phase 2 drivers. **This is the first change that needs a prompt loader** — must be built from scratch.
3. The proposal does not list `puresnmp-crypto` as a runtime dep. puresnmp v2 requires it for v3 auth+priv (DES/AES), per context7 docs (`/exhuma/puresnmp`). Without it, the v3 path is `noAuth`/`authNoPriv` only.

### Affected Areas

- `src/nora/drivers/` — **new** package, six `.py` files (`{__init__,inventory,oid_catalog,registry}.py` + `snmp_pmp450i/{client,v2c,v3,report,driver,exceptions}.py`).
- `src/nora/prompts/` — **new** package: `snmp_pmp450i.md` system prompt + `registry.py` loader (`NORA_PROMPTS_DIR` env).
- `src/nora/config.py:43-73` — **extended** with `oid_catalogs_path`, `devices_inventory_path`, `oid_catalog_signing_key`. No behavioural change to existing fields; `extra="ignore"` (line 49) shields callers that don't set them.
- `src/nora/server.py:38-141` — **modified**: add `@mcp.tool snmp_get_pmp450i_radio_metrics(device_id: str) -> dict[str, Any]`; import + wire driver facades. Stays inside the existing single-file `nora-mcp-server` pattern.
- `pyproject.toml:24-42` — **modified**: add `puresnmp==2.0.1`, `puresnmp-crypto` (runtime); `snmpsim` (dev). `pydantic-settings>=2` already covers `BaseSettings` for `Device` model.
- `.env.example` — **extended** with three new keys, sanitised placeholders (RFC 5737 docs IPs, `change-me` for the signing-key path).
- `data/oid-catalogs/cambium/pmp450i/<firmware>.json` — **new**, public sanitised catalog entries (placeholder structure plus numeric OID list).
- `data/devices.example.yaml` — **new**, public example inventory; `.env` example analogues.
- `tests/test_driver_snmp_pmp450i_*`, `tests/test_oid_catalog.py`, `tests/test_inventory.py`, `tests/test_prompts.py` — **new** (mirror under `tests/drivers/`).
- `tests/test_server.py` — **modified** for the new tool's typed-return / sanitizer-boundary contract.
- `openspec/changes/phase2-pmp450i-driver/specs/{driver-snmp-pmp450i,oid-catalog,prompt-registry}/spec.md` — **new** delta specs (note: prompt-registry is a *third* new capability the proposal omits; without it the driver has no way for the LLM to know it can pick the tool).

### Approaches

1. **SNMP library — `puresnmp==2.0.1` + `puresnmp-crypto`** *(proposal choice; works)*
   - Pros: pure-Python, no libsnmp native dep, async-native, MIT-licensed, `V2C/V3` security objects parameterised exactly for this driver. Context7 confirms `Client(host, security, port, timeout=...)`, `await client.get(oid)`, `async for varbind in client.walk(oid)`. `auth/priv` methods configurable per call.
   - Cons: requires a second package (`puresnmp-crypto`) for v3 auth+priv. Proposal omits it → would have to amend.

2. **SNMP library — `easysnmp`** *(alternative)*
   - Pros: pysnmp-backed, mature, C-extension often pre-installed on Linux NOC hosts.
   - Cons: wraps `net-snmp`/`libsnmp` native libs → breaks `uv sync` reproducibility (a Phase 1 hard rule, `openspec/specs/project-toolchain/spec.md` R1). Hard fail.

3. **SNMP library — `pysnmp` (asyncio)** *(alternative)*
   - Pros: pure-Python, BSD-3, the canonical Python SNMP stack.
   - Cons: async API exists but uses complex `cmdgen/CommandGenerator` + `UdpTransportTarget` ceremony; puresnmp is the provenly simpler interface for the single-driver scope.

4. **OID catalog format — JSON vs YAML**
   - Pros for JSON: matches proposed `data/oid-catalogs/{vendor}/{model}/{firmware}.json`; unit-testable with stdlib `json`; cacheable by content hash for the integrity signature; provenance metadata sits next to OIDs in one file.
   - Cons for JSON: no comments (schema docs live in spec, not file).
   - YAML: no advantage here — requires a new dep, more parsing surface, no schema-validation advantage with our pydantic side.
   - **Pick JSON.** No new dep.

5. **Catalog integrity — libsodium sealed_box vs `cryptography` ed25519 vs HMAC-SHA256**
   - HMAC-SHA256 over a per-release symmetric key is the cheapest match: the signing key is a single `nora_oid_catalog_signing_key` byte string the operator loads at boot, and verification is `hmac.compare_digest(...)`. Ed25519 is asymmetric (better key hygiene for external signers but needs an external signer) — heavier than a first-slice driver needs. libsodium is another native lib, breaks reproducibility. **Pick HMAC-SHA256.**

6. **Prompt registry — file-system watcher vs one-shot boot loader vs MCP resource**
   - The proposal says "boot-load + `NORA_PROMPTS_DIR`". One-shot boot loader is the simplest match: read each `*.md` at boot, hash the resolved path, treat override-via-env as a per-process rotation through restart. An MCP `resource` lookup is overkill for one prompt per driver type. **Pick one-shot boot loader, no watcher.**

### Recommendation

Ship `phase2-pmp450i-driver` as a single change behind three new capability specs (`driver-snmp-pmp450i`, `oid-catalog`, `prompt-registry`). The driver package lives at `src/nora/drivers/`, prompt registry at `src/nora/prompts/`. Use `puresnmp==2.0.1 + puresnmp-crypto` (add both to `[project.dependencies]`), `snmpsim` in `[dependency-groups].dev`, JSON catalogs with HMAC-SHA256 integrity, one-shot prompt loader.

**Reconcile the proposal before `--propose` formalises it:**
- Remove the "depends on `phase2-session-journal` (must be archived first)" dependency note — that change is **archived** as of 2026-09-10.
- Add `prompt-registry` to the new-capabilities list (proposal currently lists only two; the third is implied).
- Add `puresnmp-crypto` to the new-runtime deps alongside `puresnmp==2.0.1`.
- Replace the "LLM discovers the tool via `provider.model.act(...)`" line with the honest Phase 1 wire: tools are exposed in the MCP schema; the operator's MCP client (e.g., Claude Desktop, LM Studio MCP plugin) routes the LLM's tool choice to our `@mcp.tool`. No SDK call on our side.
- Drop the "Updated text: once that change lands" wording around `nora_session_set_focus` — the tool is on `main` now (`src/nora/server.py:175-183`).
- Add a section on **read-only enforcement at the driver surface** — `RefusesWriteError` is the right name; pair it with a public-API property test that any method name matching `(set|update|write|bulk_set|setbulk)` raises it. Mirror the air-gap static scan from `tests/test_session_journal_airgap.py` for the "no `requests/httpx/urllib.request/socket/ssl/http.client` in `src/nora/drivers/`" guarantee (R8-style).
- Add a third capability spec for the **prompt registry** so the LLM-side wiring has a spec of record; the proposal currently leaves `src/nora/prompts/registry.py` as in-line code only.
- The "Deliverable work units / chained PRs" question belongs to `sdd-tasks`. Forecast: one PR slice is feasible if vendor OID selection, puresnmp facade, MCP tool, prompt registry, sanitisation boundary, and air-gap gate stay under 800 lines together. Split along `inventory` → `client+v2c+v3` → `tool+server` if `sdd-tasks` reports a Medium/High risk.

**Required ordering in `sdd-spec` then `sdd-design`**: lock down the `RadioMetricsReport` Pydantic schema first (the typed shape Phase 3 will reuse as `ComponentState`), then the OID catalog integrity contract, then the prompt contract, then the tool surface. Otherwise the design will re-litigate types after prompts and inventory get welded in.

### Risks

- **OID drift across PMP 450i firmware revisions** — Cambium renumbers when firmware ships; a wrong OID returns silently-zero data. **Mitigation:** `CatalogNotFoundError` on unknown firmware → refuses to boot a device; catalog is signed and pinned to `device.firmware`; new firmware = new catalog entry, never a silent forward.
- **Vendor MIB licensing** — Cambium's WHISP-SM-MIB ships under their EULA; we cannot redistribute it. **Mitigation:** our catalogs are derived from public OID assignments only, hand-curated from documented object names (`whispSM...`) plus Cambium community forums. Do **not** vendor the MIB. Probe the test team with the licenses question before they sign off.
- **Air-gap boundary regression** — a future contributor reaches for `httpx` to add "just one MIB fetch". **Mitigation:** mirror the air-gap static+runtime test pattern from `tests/test_session_journal_airgap.py:114-153`; add `src/nora/drivers/` to the AST scan; review-gate `pysnmp` (it pulls in `socket`); only `puresnmp + stdlib + pydantic` allowed under `drivers/`.
- **Read-only guarantee regression** — someone later adds `.set()` to the driver. **Mitigation:** property test enumerates the public driver API; explicit `RefusesWriteError` test; AST lint check that any `set|update|write|bulk_set` identifier raises.
- **`snmpsim` lockfile churn** — `snmpsim` pulls `pysnmp` transitively, increasing `uv.lock`. **Mitigation:** declare it as `[dependency-groups].dev` only (no runtime contamination), and re-run `uv lock --check` in CI to catch drift.
- **LLM mis-routes `device_id`** — wrong alias → bootstrap with a malformed config key. **Mitigation:** Pydantic validation rejects unknown devices with `DeviceNotFoundError`; the device_id never crosses the sanitizer boundary inside `RadioMetricsReport` (it's typed), but a free-text *error* message does — apply `Sanitizer` to error text via the same module-level `_sanitizer` at `src/nora/server.py:45`.
- **`pytest-asyncio` accidentally introduced** — proposals sometimes reach for it; this project deliberately doesn't use it (tests use `asyncio.run` wrappers). **Mitigation:** explicit `Dependency-review` step on `pyproject.toml` during `sdd-apply`; reject any `[tool.pytest.ini_options] asyncio_mode` change.
- **Coverage 85% not held once `snmpsim` integration lands** — wire-format tests are thicker; unit tests for typed-report folding carry more weight. **Mitigation:** split unit/typed-contract vs integration/wire in `sdd-tasks`; integration tier must not gate CI by default (mark `@pytest.mark.slow`).
- **`puresnmp-crypto` forgotten in proposal** — v3 path returns `noAuth` silently. **Mitigation:** add `puresnmp-crypto` to the new-runtime dep list; integration test for v3 auth+priv blocks merge if absent.

### Ready for Proposal

**Yes — proceed to `sdd-propose` after reconciling the four gaps above (no in-flight dependency, missing `prompt-registry` capability, missing `puresnmp-crypto`, wrong `model.act` reference).**

The orchestrator should tell the user:

1. The proposal text in `openspec/changes/phase2-pmp450i-driver/proposal.md` is structurally sound but has four concrete mismatches against current `main` (reconcile them in the file *before* `sdd-propose` runs, or `sdd-propose` will write its own version).
2. There are six genuinely independent choices the design phase can still refine; the explore hit recommends (a) `puresnmp+puresnmp-crypto`, (b) JSON catalogs, (c) HMAC-SHA256 integrity, (d) one-shot prompt loader.
3. The dependency on `phase2-session-journal` is **satisfied** — that change is archived. Future revisions of this proposal should reference the archived spec, not a non-existent in-flight change.
4. The 85% coverage gate (`openspec/config.yaml`) and the 800-line PR-review budget (`make test` rule + project-toolchain spec) make this change viable as **one or two PR slices** depending on whether `puresnmp` facade, driver, MCP tool, prompt registry, and tests fit under 800 lines. Forecast in `sdd-tasks`.
