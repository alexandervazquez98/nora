# Proposal: PMP 450i SNMP Driver (Phase 2 — Driver Layer)

## Intent

Operators SSH into each PMP 450i SM and decode WHISP-SM-MIB OIDs by hand. This change ships the **first read-only SNMP driver** for Cambium PMP 450i — strictly typed `RadioMetricsReport`, MCP-exposed, sanitized — so Phase 3 HITL reuses it as `ComponentState`. SCOPE.md §4-A; `phase2-session-journal` archived, consumed.

## Scope

**In:** v2c + v3 over `puresnmp==2.0.1` + `puresnmp-crypto`; typed `RadioMetricsReport`. HMAC-SHA256 catalogs. `Device` + inventory; additive `Settings`. FastMCP tool `snmp_get_pmp450i_radio_metrics`. `PromptRegistry` + `snmp_pmp450i.md`; `NORA_PROMPTS_DIR`. Strict TDD: unit + `snmpsim`.

**Out:** Any write op (SCOPE §4-A). Switch/SSH, ICMP, PTP 450/650, PMP 450, `ActivityReport` (Phase 3). Runtime internet, MIB-fetch, MIB vendoring (EULA).

## Capabilities

**New:**
- **`driver-snmp-pmp450i`**: read-only v2c+v3 driver, `RadioMetricsReport`, inventory, MCP tool, read-only enforcement.
- **`oid-catalog`**: air-gapped JSON catalog, per-firmware pins, HMAC-SHA256, boot-time verify.
- **`prompt-registry`**: one-shot-at-boot loader, `NORA_PROMPTS_DIR`, ships `snmp_pmp450i.md`.

**Modified:** None — `secure-configuration` gains additive `Settings`; `nora-mcp-server` typed-return inherited.

## Approach

Boot: load inventory → resolve `device.oid_catalog_ref` → verify HMAC → one GET per OID via `puresnmp.Client` → fold into `RadioMetricsReport`. One `Client` per version; one facade. Phase 1 wires `LMStudioProvider.model.respond(...)` (not `model.act`); routing is client-side. Tests use `snmpsim` in-process.

## Affected Areas

- `pyproject.toml`, `uv.lock`, `.env.example`, `src/nora/config.py`, `server.py` — `puresnmp` + `puresnmp-crypto` runtime; dev: `snmpsim`; three additive `Settings`; `@mcp.tool` + sanitizer.
- `src/nora/drivers/{inventory,oid_catalog,registry}.py` + `snmp_pmp450i/{client,v2c,v3,report,driver,exceptions}.py` — **New**: driver modules.
- `src/nora/prompts/{registry.py,snmp_pmp450i.md}` — **New**: boot loader + first prompt.
- `data/oid-catalogs/cambium/pmp450i/<firmware>.json`, `data/devices.example.yaml` + `tests/test_*` — **New/Modified**: public fixtures; strict TDD; `snmpsim`.

## Risks

| Risk | Lik | Mitigation |
|------|-----|------------|
| v3 silent fail / OID drift / read-only regress. | Med | Pin `puresnmp-crypto`; per-firmware pin; property test + `RefusesWriteError`; AST lint forbids write identifiers. |
| Air-gap / WHISP-SM-MIB EULA / `pytest-asyncio` slip / 85% drop. | Low-Med | Mirror `test_session_journal_airgap.py` AST scan over `drivers/`; catalogs from public object names only; reject `asyncio_mode`; mark integration `@pytest.mark.slow`. |

## Rollback Plan

Delete `src/nora/drivers/`, `src/nora/prompts/`, `data/oid-catalogs/`, `data/devices.example.yaml`; revert `server.py`, `config.py`, `pyproject.toml`, `uv.lock`, `.env.example`. Phase 1 + SessionJournal untouched; no secrets to scrub.

## Dependencies

- **Runtime/Dev**: `puresnmp==2.0.1`, `puresnmp-crypto`, `snmpsim` (dev).
- **Reused**: Phase 1 (`pydantic-settings`, `lmstudio`, `fastmcp`, `Sanitizer`, `Settings`); SessionJournal tools; R10 redacts `community|auth_password|priv_password`.
- **Locked (Engram)**: `puresnmp` (`obs-6766be9c29f65194`); boot-load (`obs-c0a0f25700260a4d`); catalog (`obs-4a8e9d3b65dabc81`); typed report (`obs-b9cc1ad585d5922b`).

## Success Criteria

- [ ] `pytest --cov=src/nora` exits 0; coverage ≥ 85%; `ruff`, `mypy --strict src/nora` exit 0.
- [ ] `snmp_get_pmp450i_radio_metrics(device_id)` returns Pydantic-typed `RadioMetricsReport` (no `dict`/`Any`); `snmpsim` passes for v2c + v3 (auth+priv).
- [ ] Read-only property test + `RefusesWriteError` pass; signed catalog verifies on boot (tampering → typed exception); no IPv4/MAC/serial/hostname literal in free-text MCP output.
- [ ] No runtime internet call under `drivers/` (AST lint); diff under 800-line budget; no IPs/MACs/serials/hostnames/credentials in any artifact.