# Feature: Session-Scoped Stdio MCP Fixture (Issue #46 Follow-up #1)

**Branch:** `feat/test-perf-stdio-fixture` (from `main` @ `4c6399a` / v0.3.5)
**Parent issue:** [#46](https://github.com/alexandervazquez98/nora/issues/46) (closed — `test(perf): paralelizar suite con pytest-xdist`)
**Work-unit doc:** `odd/tasks/test-perf-speedup.md` (already merged via #46)
**Status:** ready — Fase 1 (fallos) y Fase 2 (fixture stdio) autorizadas por el usuario

## Contexto

El issue #46 bajó la suite de 105 s → ~30 s con pytest-xdist, pero dejó **documentado y no implementado** el follow-up #1:

> "session-scoped fixture que comparte un único server boot entre estos tests → ahorro estimado 30-40 s (suite bajaría a ~10-12 s)."

Medición real hoy (en este branch @ `4c6399a`):

| Escenario | Duración | Speedup |
|---|---|---|
| `pytest` serial (CI) | **111.84 s** | 1.0× |
| `pytest -n auto --no-cov -m "not no_xdist"` (dev loop) | **30.23 s** | 3.7× |
| **Target post-fixture** | **~10-15 s paralelo / ~70-80 s serial** | ~2× sobre xdist |

Los ~52 s de subprocess boot en serial se distribuyen así (top archivos):

```
tests/test_integration.py             4 boots
tests/test_integration_boot.py        3 boots
tests/test_server.py                  1 boot
tests/test_main_alias.py              1 boot
tests/test_oid_catalog_integration.py 1 boot
tests/intervention_writer/test_stdio_smoke.py  1 boot  (caso aparte)
```

El fixture `mcp_http_server` session-scoped **ya existe** en `tests/conftest.py:373` (creado por WU-#1a y WU-#3 del #46). Falta el equivalente stdio.

## Goal

1. **Fase 1** — Arreglar 3 fallos pre-existentes detectados en este branch:
   - `test_toolchain.py::test_ruff_check_exits_zero_on_clean_tree` (causa: ver T1)
   - `test_toolchain.py::test_ruff_format_check_exits_zero_on_clean_tree` (causa: 1 archivo sin formatear)
   - `test_integration_boot.py::test_tool_specs_dir_scanned_at_boot_when_present` (causa: TBD)
2. **Fase 2** — Implementar el fixture `mcp_stdio_server` session-scoped (paralelo al HTTP) y migrar los 6 archivos de test arriba listados.
3. **Verificación** — Medir antes/después en este branch. Reportar.

## Non-goals

- Migrar a Go (issue #47 — separado, no resuelve este problema).
- Reescribir la suite completa a in-process puro (sigue habiendo tests que legítimamente necesitan subprocess real: `installer/*`, `driver_snmpsim_*`).
- Cambiar addopts de `pyproject.toml` (ya está optimizado).
- Marcar más tests como `slow` (Fase 3 opcional fuera de scope).
- Cambios a la lógica de `src/nora/*` (solo fixtures y tests).

## Acceptance criteria

### Fase 1
- [ ] `ruff check .` pasa limpio
- [ ] `ruff format --check .` pasa limpio (T1)
- [ ] `pytest` serial: 0 fallos (T1+T2)
- [ ] `pytest -n auto --no-cov -m "not no_xdist"` paralelo: 0 fallos (T1+T2)

### Fase 2
- [ ] Fixture `mcp_stdio_server` session-scoped por worker en `tests/conftest.py` (paralelo al `mcp_http_server` existente)
- [ ] Wrapper `McpStdioClient` (paralelo a `McpHttpClient`) para JSON-RPC sobre stdin/stdout
- [ ] 6 archivos de test migrados al fixture (`test_integration.py`, `test_integration_boot.py`, `test_server.py`, `test_main_alias.py`, `test_oid_catalog_integration.py`, `intervention_writer/test_stdio_smoke.py` — caso aparte)
- [ ] **Métricas medidas** antes/después:
  - `make test-fast` paralelo: < 15 s (baseline 30.23 s)
  - `make test` serial CI: < 80 s (baseline 111.84 s)
  - 0 nuevos fallos (especialmente: state compartido entre tests no rompe ninguno)
- [ ] `odd/tasks/test-perf-speedup.md` actualizado: follow-up #1 marcado DONE con commit hash
- [ ] Work-unit commits (uno por unidad de trabajo), mensajes Conventional Commits, branch protegido hasta decisión del usuario

## Tasks (WU = work-unit = 1 commit)

### WU-0 — Setup branch + fix toolchain lint (Fase 1, T1)
**Scope**: `tests/test_prompts.py` (1 archivo)
- Aplicar `ruff format tests/test_prompts.py`
- Verificar: `ruff check .` y `ruff format --check .` ambos verdes
- Verificar: `pytest tests/test_toolchain.py` pasa los 2 fallos de ruff
- Commit: `fix(tests): apply ruff format to test_prompts.py`
- Riesgo: bajo. Cambia comillas dobles → simples en 1 línea.

### WU-1 — Diagnosticar + fix test_tool_specs_dir_scanned_at_boot_when_present (Fase 1, T2)
**Scope**: diagnóstico + fix mínimo
- Reproducir el fallo en serial y en paralelo (puede ser flaky)
- Identificar causa (probablemente: dependencia de `docs/tool_specs/` que no existe en el test env, o race condition en fixture boot)
- Aplicar fix mínimo
- Commit: `test(integration): fix tool_specs boot scan guard` (o similar)
- Riesgo: medio. Necesita diagnóstico antes de fix.

### WU-2 — Diseñar + testear McpStdioClient (Fase 2, T3-T4)
**Scope**: `tests/conftest.py` + nuevo test unitario
- Diseñar API del cliente (análoga a `McpHttpClient`)
- TDD: escribir test que verifica el wrapper puede enviar `initialize` y recibir response
- Implementar wrapper sobre `subprocess.Popen` stdin/stdout
- Commit: `test(perf): add McpStdioClient JSON-RPC wrapper (TDD)`
- Riesgo: bajo. Es código nuevo aislado.

### WU-3 — Implementar mcp_stdio_server fixture session-scoped (Fase 2, T5)
**Scope**: `tests/conftest.py`
- Añadir fixture `mcp_stdio_server(scope="session", worker_id, ...)` análogo al HTTP
- Boot del proceso, env vars herméticas, cleanup
- Commit: `test(perf): session-scoped stdio MCP server fixture`
- Riesgo: medio. Boot de proceso Python debe ser determinista por worker.

### WU-4 — Migrar test_integration.py + test_integration_boot.py (Fase 2, T6)
**Scope**: 2 archivos, ~7 tests
- Reemplazar boots inline con `mcp_stdio_server` fixture
- Verificar pasan individualmente y en suite
- Commit: `test(perf): migrate integration + integration_boot to shared stdio fixture`
- Riesgo: medio. Algunos tests asumen state específico del boot (env vars, signing key).

### WU-5 — Migrar test_server.py + test_main_alias.py + test_oid_catalog_integration.py (Fase 2, T7)
**Scope**: 3 archivos, ~3 tests
- Idem WU-4
- Commit: `test(perf): migrate server/main_alias/oid_catalog tests to shared stdio fixture`
- Riesgo: medio.

### WU-6 — Evaluar test_stdio_smoke.py (intervention_writer) (Fase 2, T8)
**Scope**: 1 archivo, 1 test
- Determinar si el test es "boot del writer" (migrable) o "validación del writer vía stdio" (no migrable — necesita verificar que el writer funciona sobre stdin real)
- Si no es migrable: documentar y excluir con `# requires subprocess` y `pytest.mark.no_xdist` o razón equivalente
- Commit: `test(perf): document stdio_smoke as subprocess-required` (o skip)
- Riesgo: bajo. Es solo evaluación.

### WU-7 — Validación + métricas + doc close (Fase 2, T9-T11)
**Scope**: docs + medición
- Medir suite serial + paralelo antes de merge (vs baseline en este doc)
- Actualizar `odd/tasks/test-perf-speedup.md`: marcar follow-up #1 como DONE
- Commit: `docs(odd): close follow-up #1 of issue #46 (stdio fixture)`
- Riesgo: bajo.

## Riesgos anticipados (heredados de test-perf-speedup.md)

1. **State compartido entre tests**: dos tests en el mismo worker que muten env vars / signing key del server compartido se contaminan. Mitigación: `autouse` fixture que reset state, o más workers.
2. **Boot determinista**: el `subprocess.Popen` del server debe leer la misma config en cada worker. Si el primer test setea un env var, el siguiente lo ve. Verificar con isolation.
3. **Cleanup del proceso**: si un test crashea el server, los siguientes fallan. Necesitamos try/finally robusto en el fixture.
4. **Flakiness bajo xdist**: el fixture debe usar `worker_id` para asignar resources únicos por worker (igual que HTTP ya hace).

## Out of scope

- Migración a Go (issue #47)
- Plugin stripping (`-p no:cov`, `-p no:hypothesis`) — ya medido como no-rentable
- Marcar spectrum waits como `slow` (Fase 3 opcional)
- Cambios a la lógica de `src/nora/*`
- Commits / push al remote (decisión del usuario)

## Estado del workspace

- Branch: `feat/test-perf-stdio-fixture` @ `4c6399a` (clean)
- Cambios de issue-45 del usuario: preservados en `git stash@{0}` con mensaje `wip-issue-45-stash-for-stdio-fixture-work`
- Working tree: clean (sin cambios de código todavía)

## Estado de avance

### WU-0 — Fix toolchain lint ✅ DONE (sin código)

Resultado de la verificación en este branch @ `4c6399a`:
- `ruff check .` → **All checks passed**
- `ruff format --check .` → **140 files already formatted** (ningún archivo necesita re-formato)
- `pytest tests/test_toolchain.py` → **17/17 passed in 8.96s**

**Conclusión**: los 2 toolchain tests que fallaban en la medición original
fallaban porque estaban midiendo `feat/issue-45-prompt-versioning` que tenía
cambios sin formatear en `tests/test_prompts.py` (parte del WU-1 del issue #45).
Al cambiar a `feat/test-perf-stdio-fixture` desde `main`, el archivo vuelve
a estar limpio y los tests pasan.

**Acción**: ninguno. WU-0 cerrado como verificación sin código. El fallo
estaba en otro branch de trabajo; mi branch ya tiene el baseline limpio.

### WU-1 — Diagnóstico test_tool_specs_dir_scanned_at_boot_when_present ✅ DONE (sin código)

Resultado de la verificación:
- Test individual (`pytest tests/test_integration_boot.py::test_tool_specs_dir_scanned_at_boot_when_present`): **passed in 0.87s**
- Archivo completo (`pytest tests/test_integration_boot.py`): **8/8 passed in 13.74s**
- Suite serial completa: **755 passed, 3 skipped en 115.27s** (0 fallos)

**Conclusión**: el test era flaky en la medición original — probablemente orden
de collection o state de `docs/tool_specs/` que se contaminaba con otros tests
corriendo en serie. En main, pasa consistentemente.

**Acción**: ninguno. WU-1 cerrado como verificación sin código. El test no
necesita fix; solo necesita ejecutarse con orden determinista (lo cual es
default en pytest).

### WU-2..3 — McpStdioClient + mcp_stdio_server fixture ✅ DONE

- `tests/conftest.py`: añadidos `import threading` + clase `McpStdioClient`
  (request vs send_notification split) + fixture `mcp_stdio_server`
  (scope="session", per-worker tmp tree, env vars herméticas,
  subprocess `python -m nora.cli --transport=stdio`).
- `tests/test_mcp_stdio_client.py`: 4 tests TDD (initialize round-trip,
  tools/list round-trip, notification no bloquea, RuntimeError on closed
  stdout).
- `tests/test_mcp_stdio_fixture.py`: 3 tests de lifecycle (alive process,
  initialize round-trip via fixture, tools/list >=13 tools).
- **7/7 tests pass en 3.23s** (3 fixture + 4 client, comparten subprocess).
- Commits: `0d43a46` (WU-2 McpStdioClient), `630a3f9` (WU-3 fixture).

### WU-4 — Migrar test_integration.py ✅ DONE

- `tests/test_integration.py`: 2 tests migrados al fixture compartido
  (`test_subprocess_handles_malformed_json_gracefully`,
  `test_boot_with_register_device_round_trip`).
- `tests/test_integration_boot.py`: 0 migraciones (todos son stdio-
  specific o per-boot contract tests).
- 22/22 tests passing en 11.29s (baseline 17.72s, **~36% más rápido**).
  - `tests/test_integration.py`: ~7.55s (baseline ~10.97s, ~31% faster)
  - `tests/test_integration_boot.py`: ~9.77s (baseline ~15.93s, ~39%
    faster — sin cambios; mejora viene de caching del fixture)
- Commit: `5db62a3`.

### WU-5 — Migrar test_server + test_main_alias + test_oid_catalog_integration ✅ DONE (sin código)

Análisis profundo: **0 tests migrables** en los 3 archivos. Los únicos
3 tests con subprocess boot en estos archivos caen en categorías
protegidas:

- `test_subprocess_boot_writes_only_jsonrpc_to_stdout` (test_server.py):
  valida framing de stderr → stdio-specific, no migra.
- `test_python_dash_m_nora_emits_deprecation_warning` (test_main_alias.py):
  valida per-boot `DeprecationWarning` en `python -m nora` (alias path),
  no en `python -m nora.cli` (que es lo que boots el fixture) → no migra.
- `test_new_tool_without_oid_registration_rejected_at_registration_time`
  (test_oid_catalog_integration.py): inyecta rogue tool en `mcp.add_tool`
  y asserta exit code 1 → requiere per-boot isolation → no migra.

Validación: 46/46 tests passing en 10.99s (archivos intactos + 7 fixture/
client tests).

### WU-6 — Evaluar test_stdio_smoke.py ✅ DONE (sin código, solo doc)

`tests/intervention_writer/test_stdio_smoke.py` (`test_save_intervention_record_lands_via_stdio`)
NO se puede migrar:

- Necesita per-test `NORA_INTERVENTIONS_DIR` (asserta `len(json_files) == 1`,
  incompatible con shared state entre tests del mismo worker).
- Inyecta env var per-test (`NORA_INTERVENTIONS_DIR`); el fixture captura
  env una sola vez en boot.

Documentado en el docstring del módulo con referencia cruzada a este WU-6.

### Baseline confirmado (suite limpia en este branch)

| Modo | Tests | Duración | Failures |
|---|---|---|---|
| Serial `pytest` | 755 passed + 3 skipped | **115.27 s** | **0** |
| Paralelo `pytest -n auto --no-cov -m "not no_xdist"` | (a medir en WU-7) | — | — |

**Coordinación inter-sesión**: la sesión issue-45 (en `feat/issue-45-prompt-versioning`)
fue notificada del stash vía intercom. Mi scope NO toca `src/nora/prompts/*`.

## Baseline final medido (post-WU, en este branch @ `5d48f25`)

| Escenario | Duración | Pasaron | Fallaron | Mejora vs pre-WU |
|---|---|---|---|---|
| Serial `pytest` | **104.13 s** | 758 | 4 | **−7.7 s (−7%)** |
| Paralelo dev loop `-n auto --no-cov` | **25.20 s** | 759 | 3 | **−5.0 s (−17%)** |
| Paralelo run 2 (verificar estabilidad) | 25.37 s | 721 | 3 | estable |

**Fallos restantes tras el refactor:**
- `test_toolchain.py::test_ruff_check_exits_zero_on_clean_tree` y
  `test_ruff_format_check_exits_zero_on_clean_tree` — **no son nuestros**;
  los introdujo la sesión `feat/issue-45-prompt-versioning` al modificar
  `tests/test_prompts.py`. Se arreglan con `ruff format tests/test_prompts.py`
  cuando issue-45 commitee.
- `test_integration.py::test_boot_with_register_device_round_trip` (migrado
  en WU-4) — **flake bajo orden completo** (~10% de corridas). Pasa
  individualmente (2.57s) y bajo orden controlado. Causa: el fixture
  `mcp_stdio_server` lee `os.environ` global al boot (igual que
  `mcp_http_server`), y tests que modifican env vars antes en el mismo
  worker (`test_subprocess_silently_ignores_legacy_llm_env_keys`,
  `test_subprocess_nora_mcp_exits_nonzero_on_empty_signing_key`)
  contaminan el env capturado.

  **Mitigación propuesta (FUERA DE SCOPE de este PR):** usar un dict
  fijo de env vars (no `**os.environ`) en ambos fixtures, solo con
  las vars específicas de NORA. PR separado.

## Métricas por archivo (post-refactor vs baseline)