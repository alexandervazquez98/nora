# Tasks: PMP 450i SNMP Driver (Phase 2 — Driver Layer)

## Review Workload Forecast

| Field | Value |
|-------|-------|
| Estimated changed lines | 2500–3000 |
| 400-line budget risk | High |
| Chained PRs recommended | Yes |
| Suggested split | PR 1 → PR 2 → PR 3 → PR 4 |
| Delivery strategy | ask-on-risk |
| Chain strategy | pending |

Decision needed before apply: Yes
Chained PRs recommended: Yes
Chain strategy: pending
400-line budget risk: High

### Suggested Work Units

- **PR 1.** Test `pytest tests/test_driver_exceptions.py -q`. Runtime N/A. Rollback: delete `src/nora/drivers/{__init__.py,exceptions.py}`; revert deps.
- **PR 2.** Test `pytest tests/test_oid_catalog.py tests/test_prompts.py -q`. Runtime N/A. Rollback: delete `src/nora/prompts/`, `data/oid-catalogs/`, `oid_catalog.py`; revert `Settings`.
- **PR 3.** Test `pytest tests/test_inventory.py tests/test_driver_snmp_pmp450i.py -q`. Runtime Hypothesis. Rollback: delete `src/nora/drivers/{inventory,snmp_pmp450i}/`.
- **PR 4.** Test `pytest tests/test_server.py tests/test_driver_airgap.py -q` + `-m slow`. Runtime `snmpsim`. Rollback: revert `server.py`, `__main__.py`; delete new tests.

## Phase 1: Foundation

- [x] 1.1 `pyproject.toml`: add `puresnmp==2.0.1`, `puresnmp-crypto`, `snmpsim` (dev). No `pytest-asyncio`.
- [x] 1.2 RED `tests/test_driver_exceptions.py` — hierarchy.
- [x] 1.3 GREEN `src/nora/drivers/exceptions.py` — 8 typed exceptions inheriting `DriverError`.
- [x] 1.4 Empty `src/nora/drivers/{__init__.py,snmp_pmp450i/__init__.py}`; re-export `DriverError`.

## Phase 2: Catalog + Prompt

- [x] 2.1 `src/nora/config.py`: add 4 `Settings` fields; keep `extra="ignore"`.
- [x] 2.2 `.env.example`: 4 env keys (RFC 5737 + `change-me`).
- [x] 2.3 `tests/conftest.py`: `tmp_catalogs_dir`, `sample_catalog`, `tmp_prompts_dir`.
- [x] 2.4 RED `tests/test_oid_catalog.py` — OidCatalog-R1…R5 scenarios.
- [x] 2.5 GREEN `src/nora/drivers/oid_catalog.py` — `OidCatalog`, `OidCatalogRegistry.verify_all`, `REQUIRED_OIDS`, HMAC via `hmac.compare_digest`.
- [x] 2.6 `data/oid-catalogs/cambium/pmp450i/15.2.1.json` (15–20 entries; no MIB prose).
- [x] 2.7 RED `tests/test_prompts.py` — Prompt-R1…R7 scenarios.
- [x] 2.8 GREEN `src/nora/prompts/registry.py` — `Prompt`, `PromptRegistry.scan`, `get`; YAML front-matter via `yaml.safe_load`.
- [x] 2.9 `src/nora/prompts/{__init__.py,snmp_pmp450i.md}` (tool name, schema, zero-leakage rules).

## Phase 3: Driver core

- [x] 3.1 RED `tests/test_inventory.py` — `Inventory.from_yaml` round-trip; unknown id → `DeviceNotFoundError`; v2c requires `community`.
- [x] 3.2 GREEN `src/nora/drivers/inventory.py` — `Device(BaseModel)`, `Inventory.from_yaml`, `get`; `SecretStr` for credentials.
- [x] 3.3 RED `tests/test_driver_snmp_pmp450i.py` — Driver-R1, R3, R6 scenarios.
- [x] 3.4 GREEN `src/nora/drivers/snmp_pmp450i/{client.py,v2c.py,v3.py}` — `SnmpClient` Protocol; `V2CClient`/`V3Client` wrap `puresnmp.PyWrapper(Client(...))`.
- [x] 3.5 GREEN `src/nora/drivers/snmp_pmp450i/report.py` — `RadioMetricsReport` Pydantic (no `dict`/`Any`); `REQUIRED_OIDS`; `fold`.
- [x] 3.6 GREEN `src/nora/drivers/snmp_pmp450i/driver.py` — `Pmp450iDriver.fetch_radio_metrics`; `nora_session_set_focus` first; map `puresnmp.exc.*`.
- [x] 3.7 RED Hypothesis + AST lint — `SnmpClient` write-verb set empty; no write identifiers under `src/nora/drivers/` (Driver-R2).

## Phase 4: Wiring + integration

- [x] 4.1 `tests/conftest.py`: add `sample_inventory`.
- [x] 4.2 RED extend `tests/test_server.py` — Driver-R3, R4 scenarios.
- [x] 4.3 GREEN `src/nora/server.py` — `@mcp.tool snmp_get_pmp450i_radio_metrics(device_id) -> dict[str, Any]` → `Pmp450iDriver.fetch_radio_metrics`; serialise via `model_dump(mode="json")`.
- [x] 4.4 `src/nora/__main__.py`: after `init_session_journal`, run `PromptRegistry.scan` + `OidCatalogRegistry.verify_all`; extend boot seam.
- [x] 4.5 `tests/test_driver_airgap.py` mirroring `tests/test_session_journal_airgap.py` (read-only) over `src/nora/drivers/` + `src/nora/prompts/`.
- [x] 4.6 `tests/test_driver_snmpsim_v2c.py` (`@pytest.mark.slow`) — boot `snmpsim`; assert typed result + auto-trace step.
- [x] 4.7 `tests/test_driver_snmpsim_v3.py` (`@pytest.mark.slow`) — auth+priv; credentials absent from logged records (R10).
- [x] 4.8 `data/devices.example.yaml` (RFC 5737 host, `change-me`, v2c + v3).
- [x] 4.9 `pytest --cov=src/nora --cov-report=term-missing` ≥ 85%; `ruff check .` + `mypy --strict src/nora` exit 0.