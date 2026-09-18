# Feature: Test Performance Speedup (Issue #46)

**Branch:** `chore/test-perf-xdist` (rebased sobre `origin/main` @ `a371fe3`)
**Issue:** https://github.com/alexandervazquez98/nora/issues/46
**Status:** ready for handoff — implementación completa, follow-ups documentados

## Contexto

Benchmark inicial (en `main` @ `f9aea1e`) mostró:

| Escenario                              | Baseline            |
| -------------------------------------- | ------------------- |
| Suite pytest completa cold             | 1 min 45 s (105 s)  |
| 1 test file (smoke) hot                | 1.45 s              |
| `python -m nora mcp` hot               | ~1.3 s              |
| `uv sync --frozen` warm                | 30 ms               |

El cuello era la suite completa. `uv sync` ya estaba bien optimizado.

## Cambios implementados (5 archivos tracked)

1. **`pyproject.toml`** — `[dependency-groups].dev`: agregadas `pytest-xdist>=3.6`,
   `pytest-watch>=4.2`. `[tool.pytest.ini_options].addopts`: cambiado a
   `-q --strict-markers --no-cov -p no:cacheprovider`. Comentario explica
   por qué `-n auto` NO está en default (rompe single-file).

2. **`Makefile`** — Nuevos targets `test-fast` (xdist parallel, sin coverage),
   `test-one K=foo` (single test pattern), `watch` (pytest-watch con xdist).
   El target `test` original ahora corre con `--cov` explícito.

3. **`tests/test_toolchain.py::test_pytest_coverage_table_for_src_nora_is_printed`**
   — Refactorizado para correr pytest internamente contra `tests/test_smoke.py`
   en lugar de la suite completa. Razón: la corrida full-suite con `--cov` +
   xdist expone una race condition pre-existente en `test_bootstrap`
   (filesystem state) y `test_http_smoke` (TCP port binding). De **50 s → 2.5 s**
   aislado y **24× más rápido en la suite**.

4. **`.github/workflows/ci.yml`** — Creado desde cero. Doble cache (uv wheels
   + `.venv`), pre-compile bytecode, lint + format + type + sequential pytest
   con coverage. Sequential por la misma razón (race conditions en xdist).

5. **`OPERATIONS.md`** — Nueva sección "Test execution workflow" documentando
   los 3 modos (dev loop con `test-fast`/`test-one`/`watch`, pre-PR con
   `test`, CI automatizado).

6. **`uv.lock`** — Regenerado con `uv lock` para incluir las nuevas deps
   transitivas (`pytest-xdist`, `pytest-watch`, `execnet`, `watchdog`, `docopt`,
   `colorama`).

## Resultados finales (medidos en `chore/test-perf-xdist` @ `a371fe3`)

| Escenario                                      | Baseline | Final  | Mejora | Target | Cumple |
| ---------------------------------------------- | -------- | ------ | ------ | ------ | ------ |
| Suite con xdist `--dist=loadscope`             | 105 s    | **~23 s** | **4.6×** | < 40 s | ✅ |
| Suite CI (sequential, con coverage)            | 105 s    | ~40 s  | 2.6×   | n/a    | ✅     |
| Single file hot                                | 1.45 s   | 1.4 s  | igual  | < 1.0 s | ❌ (margen mínimo) |
| Coverage table test (aislado)                  | ~50 s (con race) | **2.5 s** | **24×** | n/a | ✅ |

**Lectura**: el objetivo de suite < 40 s se cumple holgadamente. Single-file
no mejoró materialmente (estructural pytest overhead); bajarlo más requiere
deshabilitar plugins (`-p no:cov`, `-p no:hypothesis`) que rompen el target
coverage en CI — no es viable.

## Hallazgos de la investigación (pytest --durations=30)

Top archivos por tiempo cumulative en suite completa (con xdist):

```
59.6s  tests/test_toolchain.py       ← ANTES del refactor: dominated por 1 test de 50s
15.5s  tests/test_integration_boot.py   ← 5+ tests con subprocess MCP boot
15.2s  tests/test_integration.py       ← 5+ tests con subprocess boot + stdio
 7.5s  tests/test_server.py             ← 2+ subprocess boot tests
 7.2s  tests/test_oid_catalog_integration.py
 6.2s  tests/installer/test_verify_install.py
 9.6s  tests/installer/test_bootstrap.py  ← setup + git clone real
```

**Patrón dominante**: muchos tests hacen `subprocess.run([python, "-m", "pytest" or "nora", ...])`,
pagan el cold start (~1.5s) + boot + teardown cada uno. **Oportunidad grande**
(NO incluida en este PR, scope creep): session-scoped fixture que comparte un
único server boot entre estos tests → ahorro estimado **30-40 s** (suite bajaría
a ~10-12 s).

## Flakiness pre-existente descubierto (no introducido por #46)

Con `pytest -n auto` (xdist) sobre la suite completa, los siguientes tests
fallan intermitentemente:

- `tests/installer/test_bootstrap.py::test_bootstrap_cleans_temp_clone_on_install_success`
  (~80% falla en xdist con full suite, pasa individualmente)
- `tests/test_http_transport_smoke.py::test_http_smoke_root_returns_any_status`
  (~30% falla por TCP port binding entre workers)

**Causa**: orden de tests no determinista + filesystem state y TCP ports
compartidos entre workers. Con `--dist=loadscope` baja a ~20%.

**Mitigación aplicada**: CI workflow NO usa xdist (sequential estable).
Documentado en OPERATIONS.md que `make test-fast` puede mostrar fallos
conocidos.

## Decisiones de diseño documentadas en el código

1. **`-n auto` NO en default `addopts`**: probamos y rompía single-file
   (5× peor). El paralelismo es opt-in via `test-fast` y `test-one`.

2. **`--no-cov` en default `addopts`**: coverage se activa explícitamente en
   `make test` y CI. El flag reduce ~50% el tiempo de suite porque evita
   instrumentación.

3. **`-p no:cacheprovider`**: el repo ya tiene sus propios caches
   (`.pytest_cache` etc.); deshabilitar el cache provider evita overhead.

4. **Coverage table test refactor**: el test original asumía que `-n auto`
   estaría en default (porque lo estaba cuando se escribió). Hacer el test
   explícito sobre su config de paralelización es mejor práctica de todos modos.

## Follow-ups recomendados (issues separadas)

1. **`test(integration): session-scoped MCP server fixture`** — Compartir
   un único subprocess boot entre `test_integration.py`, `test_integration_boot.py`,
   `test_server.py`, `test_main_alias.py`. Ahorro estimado: 30-40 s.

2. **`test(installer): git clone --depth=1`** — **YA IMPLEMENTADO** desde
   el commit `04f9588` (issue #22). `scripts/bootstrap.sh:241,244` ya usa
   `--depth 1` en ambas invocaciones. Sin acción pendiente.

3. **`test(http): session-scoped HTTP server fixture`** — `test_http_smoke`
   puede compartir server entre tests. Ahorro estimado: 2 s.

4. **`test(flakiness): mark test_bootstrap_* and test_http_smoke_* as no_xdist`**
   — Marcar tests con `@pytest.mark.no_xdist` y excluirlos cuando se corre
   xdist. Elimina el flakiness conocido sin perder cobertura.

5. **`test(perf): investigate -p no:cov -p no:hypothesis for dev loop`** —
   Explorar si se puede tener `addopts` distintos para dev vs CI. Trade-off:
   single-file ~0.4 s pero coverage solo disponible vía flag explícito.

## Estado de avance (sesión 2026-09-18)

- **WU-0** (`fix(toolchain-test)`): clear addopts en subprocess de coverage —
  aplicado en commit `94a4d38`. El flag `--no-cov` del `addopts` del padre
  envenenaba el `pytest --cov=nora` del subprocess; ahora se sobreescribe
  con `-o "addopts=-q --strict-markers"`. Suite verde en este eje.

- **WU-#4** (`test(perf)`): marker `no_xdist` para `test_bootstrap.py` (38
  tests) y `test_http_transport_smoke.py` (2 tests) — aplicado en commit
  `2105559`. `make test-fast` filtra `-m "not no_xdist"` y la suite baja
  de 23-37 s con flake intermitente a 19.2 s verde estable. CI sequential
  sigue corriendo los 40 tests marcados.

- **WU-#2** (`test(installer)`): marcado como **YA IMPLEMENTADO** —
  `bootstrap.sh` ya usa `--depth 1` desde el commit inicial. Sin acción
  pendiente.

- Pendientes: **WU-#1** (session-scoped MCP fixture, 30-40 s ahorro),
  **WU-#3** (HTTP server fixture, 2 s), **WU-#5** (perfil dev/CI addopts).

## Estado del workspace

- Branch: `chore/test-perf-xdist` @ `a371fe3` (rebased sobre origin/main)
- Working tree: 5 archivos modified (MIS cambios) + `.github/` y `odd/` untracked
- Stash: vacío
- Deps: instaladas en `.venv` (pytest-xdist, pytest-watch y transitivas)
- CI workflow: creado, YAML válido, NO commiteado todavía
- Task file: este documento

## Out of scope (no incluido en este PR)

- Refactor de tests de integración con session-scoped fixtures (issue separada)
- Marcar tests flaky pre-existentes con markers (issue separada)
- Cualquier cambio a la lógica de `src/nora/*` (no tocado)
- Commits del branch (decisión del usuario)
