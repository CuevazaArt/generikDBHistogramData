# generikDBHistogramData

Toolkit de backtesting con motor dual (Python de referencia, Rust opcional
vía `pyo3`/`maturin`), data lake **Parquet-first**, metadatos en SQLite o
PostgreSQL, orquestador paralelo con `ResourceGuard`, biblioteca de bots y
reportes en DuckDB sobre Parquet con fallback a SQLite. Mantiene la CLI y la
API de estrategias del sistema previo y las extiende con `--engine`,
`--resume`, `walk-forward`, `multi-symbol`, `pg-init`, `migrate` y la
biblioteca `library/`.

## Directivas Operativas (obligatorias)

Antes de cualquier descarga, backtest u optimización:

1. Leer este `README.md` y luego [`USER_MANUAL.md`](USER_MANUAL.md) para los flujos paso a paso.
2. Mantener la app por debajo de `90%` CPU y `90%` RAM sostenidos.
3. Para cargas pesadas (`1s` anual), preferir streaming Parquet + `--checkpoint_every_bars` y ejecutar por bloques (mensual/trimestral) si es necesario.
4. No correr varias optimizaciones pesadas contra la misma SQLite; activar PostgreSQL para concurrencia real (`BACKTEST_METADATA_BACKEND=pg`).
5. Todo entregable termina en `reports/entregables/` con `MANIFEST.md` por carpeta.
6. **Homologacion y reutilizacion de capacidades**: cada vez que se alcance o desarrolle una herramienta, accesorio o mejora que incremente las capacidades de backtest (sizing, accesorios de estrategia, curaduria de datos, orquestacion, reportes, integridad, etc.), debe quedar:
   - **Homologada** a situaciones y conjuntos de datos analogos (cualquier `symbol/interval/year`, no hardcodes 2024/XRP/Dorothy).
   - **Accesible y reutilizable** por otros bots, scripts y herramientas del proyecto (interfaz comun, manifest o subcomando CLI), sin duplicar logica.
   - **Documentada** en `README.md`/`docs/` con ejemplo y referencia desde la biblioteca (`library/`) cuando aplique.
7. **Fin ultimo de la herramienta**: la busqueda y desarrollo de **artefactos** (bots, accesorios, presets, datasets curados, indicadores, reportes) que ayuden a generar los **mayores beneficios posibles en el menor tiempo posible**. Toda decision tecnica (datos, motor, persistencia, paralelismo, reportes) se prioriza segun ese criterio.

## Quick start

```powershell
# 1. Entorno
python -m venv .venv ; .venv\Scripts\activate
pip install -r requirements.txt
# opcional para desarrollo (ruff, mypy, pre-commit, maturin):
pip install -r requirements-dev.txt

# 2. Backtest sobre el corpus existente (BTCUSDT 1h en data/klines/...)
python backtest_cli.py --db klines.db run --strategy dorothy --symbol BTCUSDT --interval 1h

# 3. Optimización paralela con joblib
python backtest_cli.py --db klines.db optimize --strategy dorothy --symbol BTCUSDT --interval 1h --study quickstart --trials 30 --executor joblib

# 4. Reporte del run resultante (DuckDB si hay Parquet, SQLite si no)
python backtest_cli.py --db klines.db plot --run_id 1 --output_dir reports
```

Recetas adicionales (resume, walk-forward, multi-symbol, migración a PG,
publicar bots) en [`USER_MANUAL.md`](USER_MANUAL.md) §14.

## Artefacto intermedio de dataset (curado/preparacion)

El comando `dataset` genera un artefacto **generico** de ventana de datos para
cualquier bot/estrategia (no solo Dorothy). Sirve para desacoplar el curado de
datos de la corrida del bot y reutilizar la misma ventana preparada en varias
ejecuciones.

Comandos principales:

```powershell
# Preparar artefacto (desde klines.db -> cache parquet o snapshot JSONL)
python backtest_cli.py --db klines.db dataset prepare --symbol XRPUSDT --interval 1s --start_ts 1735689600000 --end_ts 1767225599000 --name xrp_2025_1s

# Verificar integridad estructural del artefacto ya generado
python backtest_cli.py --db klines.db dataset verify --manifest reports/entregables/datasets/xrp_2025_1s/manifest.json
```

Salida esperada:

```text
reports/entregables/datasets/<artifact_name>/
  MANIFEST.md
  manifest.json
  window.jsonl                  # fallback si no hay cache parquet
```

`manifest.json` incluye:
- ventana solicitada (`symbol`, `interval`, `start_ts`, `end_ts`);
- archivos preparados (`prepared_data.files`), ya sea cache Parquet reutilizable
  o snapshot JSONL;
- diagnosticos de integridad (`row_count`, `gap_count`, `gaps`,
  `expected_step_ms`);
- snapshot de reproducibilidad (`reproducibility.git`) para auditar con que
  estado de repo se genero el artefacto.

Este flujo permite detectar gaps antes del backtest y repetir corridas con la
misma base de datos curada de forma trazable.

## Strict run encadenado por mes (--chain-by-month)

`scripts/run_xrpusdt_2024_dorothy_strict.py` corre la estrategia `dorothy` en
**cadena cronologica** mes-a-mes: cada mes inicia con el estado final del mes
anterior (broker + `active_sell_limits` + indicadores serializables). Esto
permite reproducir trayectorias mensuales realistas sin que un solo bloque de
~31M velas en 1s sobrepase la RAM disponible.

Desde 2026-05 el modo **es generico**: las ventanas se calculan dinamicamente
desde `--start_ts/--end_ts` (UTC ms) usando
`backtest.calendar_windows.monthly_windows`. Funciona para cualquier anio o
rango multianual; los nombres de ventana usan el formato `YYYY-MM`
(`2024-01`, `2025-12`, ...) para que sean inequivocos entre anios.

```powershell
# 2024 completo (12 meses, 2024-01..2024-12) - comportamiento equivalente al previo
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --symbol XRPUSDT --interval 1s `
  --start_ts 1704067200000 --end_ts 1735689599000 `
  --explain_only

# 2025 completo (12 meses, 2025-01..2025-12)
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --symbol XRPUSDT --interval 1s `
  --start_ts 1735689600000 --end_ts 1767225599000 `
  --explain_only

# Rango multianual (Nov-2024 -> Feb-2025, 4 ventanas)
python scripts/run_xrpusdt_2024_dorothy_strict.py --chain-by-month `
  --symbol XRPUSDT --interval 1s `
  --start_ts 1730419200000 --end_ts 1740787199000 `
  --explain_only
```

`--from-month` y `--through-month` son filtros mes-de-anio (`1..12`):

- En rangos de **un solo anio** funcionan como antes (acotan el subconjunto
  de meses).
- En rangos **multianuales** se aplican **por anio**: con
  `--from-month 11 --through-month 12` y rango `2024-01..2025-12` se generan
  `2024-11, 2024-12, 2025-11, 2025-12` (4 ventanas). Con los defaults
  `1..12` no hay filtro y el rango se cubre completo.

Las ventanas siempre quedan **clampeadas** a `[start_ts, end_ts]`, asi que
arrancar/terminar a mitad de mes produce un primer/ultimo bin parcial, sin
errores. El nombre del estudio (`study_name`) se deriva de `symbol` e
`interval` y NO esta hardcodeado a XRPUSDT/2024.

## Arquitectura en una vista

```text
+----------------------------------------------------------------------+
| Capa UX y orquestación (Python)                                      |
|   backtest_cli.py  ->  Orchestrator (Ray | joblib | serial)          |
|                        ResourceGuard adaptive_80 + Optuna            |
+----------------------------------------------------------------------+
| Capa de simulación                                                   |
|   genericbt_core (Rust opcional, pyo3/maturin)                       |
|     SpotBroker + indicadores SMA/EMA/RSI/ATR + transforms HA         |
|   backtest.engine (Python, referencia y fallback)                    |
+----------------------------------------------------------------------+
| Capa de almacenamiento                                               |
|   storage_facade  ->  SQLite (klines.db legacy) | PostgreSQL (meta/  |
|                                                  ops/optuna)         |
|   data lake Parquet:                                                 |
|     data/klines/symbol=*/interval=*/year=*/month=*/part-000.parquet  |
|     data/events/run_<id>/part-*.parquet (append-only)                |
|     data/equity/run_<id>/equity.parquet                              |
|     data/checkpoints/run_<id>/cp_<sim_ts>.json                       |
+----------------------------------------------------------------------+
| Capa de reportes                                                     |
|   backtest.plots + backtest.duckdb_reads                             |
|     auto: DuckDB sobre Parquet si está; SQLite legacy si no          |
+----------------------------------------------------------------------+
```

| Capa | Componente | Notas |
|---|---|---|
| CLI / orquestación | `backtest_cli.py`, `backtest.orchestrator` | Subcomandos `run`, `optimize`, `sweet-spot`, `walk-forward`, `multi-symbol`, `library`, `pg-init`, `migrate`. Executors `ray`/`joblib`/`serial`. |
| Simulación | `genericbt_core` (Rust) ↔ `backtest.engine` (Python) | Fallback automático a Python si la wheel no está; paridad numérica auditada. |
| Almacenamiento | `backtest.storage_facade`, `backtest.storage_paths` | SQLite o PostgreSQL para metadatos; Parquet para klines, eventos, equity, checkpoints. |
| Reportes | `backtest.plots`, `backtest.duckdb_reads`, `backtest.sweet_spot_report` | DuckDB sobre Parquet (rápido) o SQLite (legacy). Salida byte-equivalente entre backends. |

## Documentación

| Documento | Foco |
|---|---|
| [`USER_MANUAL.md`](USER_MANUAL.md) | Manual del operador: subcomandos, flags transversales, recetas paso a paso. |
| [`docs/TESTER_CAPABILITIES.md`](docs/TESTER_CAPABILITIES.md) | Referencia técnica de QA/auditoría: capacidades, garantías, suite de tests. |
| [`docs/POSTGRES.md`](docs/POSTGRES.md) | PostgreSQL local, schema, migraciones, orquestador y Optuna en PG. |
| [`docs/RUST_CORE.md`](docs/RUST_CORE.md) | Crate `genericbt-core`: build local, wheels CI, frontera Python/Rust, paridad. |
| [`docs/CHECKPOINTING.md`](docs/CHECKPOINTING.md) | Subsistema de checkpoint y resume: triggers, schema JSON, auditoría. |
| [`docs/REPORTS.md`](docs/REPORTS.md) | DuckDB sobre Parquet con fallback SQLite; aggregations pushed-down. |
| [`docs/WALKFORWARD.md`](docs/WALKFORWARD.md) | Subcomando `walk-forward`: ventanas, modos, decay, archivos generados. |
| [`docs/MULTI_SYMBOL.md`](docs/MULTI_SYMBOL.md) | Subcomando `multi-symbol`: canasta, dispersión, límite `share_cash_pool`. |
| [`docs/LIBRARY.md`](docs/LIBRARY.md) | Biblioteca de bots/indicadores/tools: estructura, manifest, DataProvider. |

## Componentes principales

- `backtest_cli.py`: CLI principal de backtesting/optimización/reportes.
- `backtest/`: motor Python, broker, indicadores, optimize, sweet-spot,
  walk-forward, multi-symbol, biblioteca, plots, storage facade, orquestador.
- `crates/genericbt-core/`: crate Rust del motor (SpotBroker + indicadores +
  loop por barra + checkpoint).
- `genericbt_core/`: shim Python que decide path Rust o fallback Python.
- `library/`: bots, indicadores y tools empaquetados con manifest + presets.
- `cli.py`: descargador de klines de Binance (API/ZIP) hacia `klines.db`.
- `service.py`, `terminal_ui.py`: API HTTP local (FastAPI) y TUI para datos.
- `scripts/`: utilidades (backup Parquet, migración a PG, levantar Postgres
  local, inspección de la DB legacy).

## Convención de Entregables

```text
reports/entregables/
  runs/run_<id>/
  studies/study_<name>/
  strict/<strict_name>/
  legacy/
  INDEX.md
```

Cada subcarpeta de run/study/strict incluye `MANIFEST.md`. La capa de
reportes resuelve automáticamente DuckDB vs SQLite y loguea
`[reports] run_id=<id> backend=...` por stderr.

## Variables de entorno relevantes

`AppConfig.from_env()` (en `backtest/config.py`) consume:

- `PG_DSN`, `BACKTEST_METADATA_BACKEND`, `BACKTEST_DATA_ROOT`,
  `BACKTEST_SQLITE_PATH`, `BACKTEST_ENGINE_KIND`, `BACKTEST_EVENTS_MODE`.

`ResourceGuardConfig.from_env()` consume:

- `BACKTEST_GUARD_CPU_CAP_PCT`, `BACKTEST_GUARD_RAM_CAP_PCT`,
  `BACKTEST_GUARD_SAMPLE_SEC`, `BACKTEST_GUARD_HIGH_WATERMARK_WINDOWS`,
  `BACKTEST_GUARD_RECOVER_WINDOWS`.

Tabla completa en [`USER_MANUAL.md`](USER_MANUAL.md) §12.

## Dev workflow

```bash
pip install -r requirements-dev.txt
pre-commit install
ruff check .
mypy backtest/
pytest tests/ -q
```

Para construir la wheel local del core Rust (opcional):

```bash
maturin develop --manifest-path crates/genericbt-core/Cargo.toml --release
```

Si Rust no está, todos los tests siguen pasando contra el motor Python; los
tests de paridad Rust se autoescapan cuando la wheel no está disponible.

## CI/CD

Workflows en `.github/workflows/`:

| Workflow | Para qué |
|---|---|
| `ci.yml` | Tests unitarios + lints en matrix Linux/macOS/Windows × Python 3.11/3.13. |
| `pg-integration.yml` | Smoke + CRUD contra `postgres:16-alpine` (service container). Aplica migraciones y prueba `meta.runs`/`meta.run_metrics`/`meta.checkpoints`. |
| `wheels.yml` | Wheels prebuiltas del crate Rust (`abi3-py311`) para manylinux x86_64+aarch64, Windows x64+x86, macOS x86_64+aarch64. Publish a PyPI preparado pero comentado (requiere OIDC trusted publishing). |
| `nightly-integrity.yml` | Validación periódica del lake Parquet + checks de integridad. |

## Hoja de Ruta Resumida

1. Mover el pre-pass de candles + persistencia Parquet a Rust (Fase 2 ya cubrió streaming + checkpoints).
2. Implementar `share_cash_pool=True` en `multi-symbol` (joint pool).
3. Mini-DSL para estrategias que evite el callback Python por barra.
4. Activar `publish:` en `wheels.yml` con OIDC trusted publishing.

## Notas de Documentación

- `README.md` es la puerta de entrada y resume la arquitectura.
- [`USER_MANUAL.md`](USER_MANUAL.md) es la fuente operativa (paso a paso).
- [`docs/TESTER_CAPABILITIES.md`](docs/TESTER_CAPABILITIES.md) es la referencia
  técnica de QA/auditoría.
- Los `docs/*.md` por fase son las fuentes de verdad por subsistema.
