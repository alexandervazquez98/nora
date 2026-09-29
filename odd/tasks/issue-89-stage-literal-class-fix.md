# Issue #89 — Cierre de la clase de bug: `stage` literal sin validar contra el enum

## Goal

Cerrar la clase de bug completa, no un síntoma. El literal de `stage` en el payload
de `save_intervention_record` nunca se validó contra el enum canónico
`src/nora/intervention_memory/models.py::Stage` en ningún punto de la cadena
spec → implementación → test. Los tres copiaron el mismo string sin confrontarlo
con la autoridad. PR #94 arregla migrate; reboot queda con el mismo defecto y un
test que lo PINTA como correcto.

## Root class

Un solo origen, dos sitios de código, una spec, un doc de operador:

1. **Spec = fuente de la clase.** `openspec/specs/pmp450i-radio-tools/spec.md`
   líneas 159, 161, 165 exigen `stage == "POST_MIGRATION"`, que NO existe en el
   enum. Quien implementó copió el literal; el test lo afirmó; PR #80/#84 lo
   arrastraron. PR #94 arregla el código pero **no toca el spec** (verificado:
   `gh pr diff 94 --name-only` = 4 archivos, ninguno spec).
2. **reboot.py:371 emite `stage="POST_REBOOT"`**, tampoco existe en el enum.
3. **El test lo fijó.** `tests/test_snmp_reboot.py:544` afirma
   `payload["stage"] == "POST_REBOOT"`, y su `spy_save` devuelve
   `{"status": "OK"}` sin validar contra el modelo. El test pasa mientras
   producción pierde el registro — el corpus fue enseñado a estar de acuerdo
   con el defecto.
4. **reboot no captura el return del writer** (`reboot.py:388`, solo
   `save_intervention_record(...)` sin asignar, bajo `except Exception`).
   Aunque se corrija el literal, el mismo silencio vuelve.

## Por qué `POST_INTERVENTION` y no un literal nuevo

El enum (`models.py:28`) es: `PRE_DIAGNOSTIC`, `SPECTRUM_ANALYSIS`,
`PRE_MIGRATION`, `SAFETY_ABORT`, `POST_MIGRATION_VERIFIED`, `POST_INTERVENTION`.

Un reboot no es una migración, así que `POST_MIGRATION_VERIFIED` es
semánticamente falso. `POST_INTERVENTION` ya existe y significa exactamente
esto. **Reutilizar un literal existente, no añadir vocabulario de wire nuevo.**
Añadir `POST_REBOOT` al enum sería una puerta de un solo sentido que los
consumidores deben implementar para siempre, para describir una operación que
ya está cubierta.

## Scope

1. **`reboot.py:371`**: `"POST_REBOOT"` → `"POST_INTERVENTION"`.
2. **`reboot.py`**: capturar el return de `save_intervention_record` y emitir
   `logger.warning` estructurado cuando `status != "OK"`, replicando el patrón
   que PR #94 ya introduce en `fetch_migrate`. Leer ese patrón primero, no
   inventar uno nuevo.
3. **`tests/test_snmp_reboot.py:544`**: el assert que fija el bug se REEMPLAZA.
   El `spy_save` pasa a validar el payload con
   `InterventionMemoryRecord.model_validate(...)` — que es exactamente lo que
   hace `save_intervention_record` por dentro. Pre-fix esto falla con
   `literal_error`; post-fix pasa. Ese es el guard real.
4. **`openspec/specs/pmp450i-radio-tools/spec.md`** líneas 159, 161, 165:
   `POST_MIGRATION` → `POST_MIGRATION_VERIFIED`.
5. **`docs/tool_specs/snmp_reboot_radio.md:24`**: `POST_REBOOT` →
   `POST_INTERVENTION`.

## Out of scope

- `odd/tasks/multi-community-migration-and-band-reboot.md:232` menciona
  `POST_REBOOT`. Es un documento de plan YA CUMPLIDO, no documentación de
  operador. No se toca. Anotar el delta si aparece.
- Tests de flake de `test_mcp_stdio_fixture` (causa raíz ya identificada y
  documentada: orden de tests sobre un subprocess `scope="session"`).
  deserves su propio issue.
- Añadir `POST_REBOOT` al enum. Explícitamente rechazado arriba.

## Route

**Delegated direct**, un writer. 4 archivos, 2 no triviales
(`reboot.py` + el test que hay que desaprender). Dispara la regla de writer.

## TDD

**Modo: Strict TDD (habilitado globalmente en CLAUDE.md). Runner: `pytest`.**

Orden obligatorio: RED → GREEN → REFACTOR.

- **RED primero**: cambiar el `spy_save` del test a validar contra
  `InterventionMemoryRecord.model_validate` y cambiar el assert a
  `POST_INTERVENTION`, con `reboot.py` intacto. El test DEBE fallar con
  `literal_error`. Si pasa sin tocar `reboot.py`, la hipótesis es incorrecta —
  parar y reportar, no seguir.
- **GREEN**: aplicar el fix de `reboot.py`.
- **REFACTOR**: el patrón de warning, si queda duplicado con migrate, evaluarlo.

No inventar evidencia. Si un test no se ejecutó, no está en rojo ni en verde.

## Verification

```bash
pytest tests/test_snmp_reboot.py -v                          # el guard nuevo
pytest tests/snmp_pmp450i/ tests/test_snmp_migrate.py \
       tests/test_snmp_reboot.py tests/intervention_writer/ \
       tests/intervention_memory/                           # sin regresiones
pytest --ignore=tests/installer                             # suite completa
pytest tests/installer
ruff check src tests && ruff format --check src tests
mypy --strict src/nora/drivers/snmp_pmp450i/reboot.py \
             src/nora/drivers/snmp_pmp450i/migrate.py
```

## Acceptance criteria

- [x] `grep -rn 'POST_MIGRATION"' openspec/specs/` → 0 resultados.
- [x] `grep -rn 'POST_REBOOT' src/` → 0 resultados.
- [x] El test de reboot falla con `literal_error` ANTES del fix (RED observado).
- [x] Suite completa verde.
- [x] `ruff check` + `ruff format --check` limpios.
- [x] `mypy --strict` limpio en ambos drivers.
- [x] Ningún test queda afirmando un literal que no está en el enum.

## Evidence ledger

| Event | When | Reference |
|---|---|---|
| PR #94 mergeable pero con 2 comentarios de spec-drift sin resolver | 2026-09-28 | issuecomment-5849… / comments de 2026-09-28T22:17:59Z |
| `grep POST_MIGRATION` → spec 159/161/165 confirmado | 2026-09-28 | shell |
| `reboot.py:371` emite `POST_REBOOT` (fuera del enum) | 2026-09-28 | shell |
| `test_snmp_reboot.py:544` afirma el literal inválido | 2026-09-28 | shell |
| `reboot.py:388` no captura el return del writer | 2026-09-28 | shell |
| Scope opción A aprobado por el usuario | 2026-09-28 | "vamos con la A" |
| RED observado — 2 tests failed con `literal_error` | 2026-09-28 | `pydantic_core._pydantic_core.ValidationError: 1 validation error for InterventionMemoryRecord\nstage\nInput should be 'PRE_DIAGNOSTIC', 'SPECTRUM_ANALYSIS', 'PRE_MIGRATION', 'SAFETY_ABORT', 'POST_MIGRATION_VERIFIED' or 'POST_INTERVENTION' [type=literal_error, input_value='POST_REBOOT', input_type=str]` |
| GREEN observado — 8 passed en test_snmp_reboot.py | 2026-09-28 | `8 passed in 0.77s` |
| Suite completa sin installer: 834 passed | 2026-09-28 | `834 passed, 28 skipped in 62.58s` |
| Suite installer: 65 passed, 9 skipped | 2026-09-28 | `65 passed, 9 skipped in 6.42s` |
| ruff check: All checks passed | 2026-09-28 | `All checks passed!` |
| ruff format: 145 files already formatted | 2026-09-28 | `145 files already formatted` |
| mypy --strict: Success | 2026-09-28 | `Success: no issues found in 2 source files` |
| Artefacto de test y lockfile fuera del commit (decisión de parent) | 2026-09-28 | `var/probes/PRB-norte-*.json` unstaged; `uv.lock` unstaged — deuda preexistente de main, va en issue aparte |
| Commit | TBD | se llena tras el commit; NO copiar el SHA del reporte del writer sin verificar |
| Push | TBD | decisión del usuario |

## Fuera de este commit — deuda preexistente de main

1. **`uv.lock` pineado en `0.3.1` mientras `pyproject.toml` está en `0.3.11`.**
   Además `httpx` figura como dev-dep en el lock aunque `pyproject.toml:35` lo
   declara runtime (issue #70, con comentario explícito de por qué).
   **No rompe CI hoy**: verificado que `uv sync --frozen` pasa en main
   (105 paquetes instalados, sin error) porque el editable install resuelve
   desde `pyproject.toml`. Es deuda cosmética, no una bomba. Corregirla aquí
   metería un bump de 10 versiones en un PR de bugfix y crearía conflicto con
   PR #94. Issue aparte.
2. **`var/` no está en `.gitignore`.** Un test de ICMP escribe
   `var/probes/*.json` a disco y el archivo se commitea si nadie lo limpia.
   Añadir a `.gitignore` es una línea y previene la recaída.
