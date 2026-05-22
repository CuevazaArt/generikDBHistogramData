# Capacidades del Tester (post-rediseño)

Referencia técnica para QA/auditoría: qué corre el tester, cómo se almacena,
qué garantiza cada subsistema y dónde mirar el test que respalda cada
afirmación. No es un manual paso-a-paso (eso vive en
[`../USER_MANUAL.md`](../USER_MANUAL.md)).

---

## 1. Engines disponibles

| Engine | Estado | Activación | Notas |
|---|---|---|---|
| **Python** | Siempre presente. Es la referencia numérica. | Default. `BACKTEST_ENGINE_KIND=python` o `--engine python`. | Loop bar-a-bar puro Python (`backtest/engine.py::run_backtest`). API estable de `StrategyBase.on_bar`. |
| **Rust** (`genericbt-core`) | Opt-in. Wheel `pyo3`/`maturin`, abi3-py311. | `BACKTEST_ENGINE_KIND=rust` o `--engine rust`. Cae a Python con warning si la wheel falta. | Hot path 1:1 con `backtest/{broker,indicators,transforms}.py`. Suelta el GIL fuera de `on_bar`. |

Verificación de paridad numérica: `tests/test_engine_rs_parity.py` usa
`math.isclose(rel_tol=1e-12, abs_tol=1e-9)` sobre las 11 métricas canónicas
(`initial_cash, final_equity, total_return, max_drawdown, sharpe, sortino,
calmar, ulcer_index, win_rate, profit_factor, num_trades`) generadas a partir
de 5000 barras sintéticas con `SmaCrossStrategy(fast=10, slow=30)`. Para
forzar un path concreto:

```bash
BACKTEST_ENGINE_KIND=python python -m pytest tests/test_engine_rs_parity.py -v
BACKTEST_ENGINE_KIND=rust   python -m pytest tests/test_engine_rs_parity.py -v
```

Detalles del crate y la frontera Python/Rust en
[`RUST_CORE.md`](RUST_CORE.md).

---

## 2. Almacenamiento

### 2.1 Lake de datos (Parquet)

Todas las rutas las construye `backtest.storage_paths.StoragePaths` y honran
`BACKTEST_DATA_ROOT` (default `data`). Escritura atómica (`.tmp` + `os.replace`).

| Artefacto | Ruta | Notas |
|---|---|---|
| Klines | `data/klines/symbol=<SYM>/interval=<INT>/year=<YYYY>/month=<MM>/part-000.parquet` | Particionado mensual. Manifest agregado en `data/klines/_manifest.json`. |
| Eventos por run | `data/events/run_<id>/part-<seq>.parquet` | Append-only. Modos `lite` / `minimal` / `full` (`BACKTEST_EVENTS_MODE`). |
| Equity por run | `data/equity/run_<id>/equity.parquet` | Curva final consolidada. |
| Checkpoints por run | `data/checkpoints/run_<id>/cp_<sim_ts>.json` | JSON intercambiable Python ↔ Rust. |
| Datasets derivados | `data/derived/<name>/...` | Para indicadores y tools de la biblioteca. |

### 2.2 Metadatos

| Backend | Activación | Tablas |
|---|---|---|
| **SQLite** (default) | `BACKTEST_METADATA_BACKEND=sqlite` o `PG_DSN` vacío. Espejo `klines.db`. | `bt_runs`, `bt_metrics`, `bt_trials`, `bt_events` (legacy + nuevos). |
| **PostgreSQL** | `BACKTEST_METADATA_BACKEND=pg` + `PG_DSN`. | Schemas `meta` (`runs`, `run_metrics`, `studies`, `trial_runs`, `trial_metrics`, `checkpoints`), `ops` (`resource_events`, `audit_log`), `optuna` (gestionado por Optuna). |

`backtest.storage_facade.get_storage()` reconcilia ambos backends; el resto
del código depende del facade, no del backend concreto.

---

## 3. Capacidades nuevas

### 3.1 Checkpoint / Resume

- **Disparadores**: cada N barras procesadas (`checkpoint_every_bars`) y/o
  cada S segundos de tiempo simulado (`checkpoint_every_sim_seconds`). El
  primero que se cumpla escribe el snapshot.
- **Snapshot**: incluye `BrokerState` completo, `strategy.export_state()`,
  `candle_offset`, `seq`, `last_exec_ts`, `last_snapshot_ts`,
  `last_trade_entry`, `engine_kind`, `engine_version`.
- **Resume**: `latest_checkpoint_path()` resuelve el archivo con `sim_ts` más
  alto. El loop reanuda exactamente en `candle_offset + 1` y emite un evento
  sintético `event_type='resume'` con el path consumido.
- **Garantías de bit-equivalencia**:
  - `tests/test_checkpoint.py::test_engine_resume_continues`: igualdad
    estricta entre un run single-shot y uno con corte/resume al medio.
  - `tests/test_checkpoint.py::test_engine_no_regression_when_disabled`:
    igualdad estricta (no `pytest.approx`) entre el motor pre-Fase-2 y
    Fase-2 cuando los flags están en `None`.
- **Auditoría**: con backend `pg`, fila best-effort en `ops.audit_log`. Sin
  PG la inserción se omite silenciosamente; el resume no depende de ella.

Detalles completos en [`CHECKPOINTING.md`](CHECKPOINTING.md).

### 3.2 Streaming Parquet

- `backtest.duckdb_reads.iter_candles_arrow_batches(...)` lee el lake por
  `RecordBatch` (row-group) en vez de materializar la serie completa en RAM.
- El engine puede operar batch-a-batch; el test
  `tests/test_streaming.py::test_streaming_matches_in_memory` audita que la
  ruta streaming produce los mismos resultados que la in-memory.
- Útil para series de varios millones de barras (1s anual ≈ 31M).

### 3.3 Orquestador paralelo

- `backtest.orchestrator.Orchestrator.map(fn, jobs)` corre cada job en un
  subprocess aislado (`backtest.worker_isolation.spawn_isolated_worker`).
- Backends: `ray` (con fallback automático a `joblib` si Ray no es
  importable), `joblib` (default en `optimize`/`sweet-spot`), `serial`.
- `ResourceGuard adaptive_80` integrado: throttle si la presión sostenida
  excede `cpu_cap_pct`/`ram_cap_pct`, scale-up con headroom sostenido.
- Cap duro de RAM por worker (`per_worker_ram_mb`): rlimit en Linux/macOS,
  watchdog psutil en Windows.
- Wall-clock por trial (`per_trial_timeout_sec`); al expirar el subprocess
  se mata limpio y queda como `FailureResult` sin tirar la batch.
- Eventos auditados:
  - `pg`: `ops.resource_events` (snapshot estructurado) + `ops.audit_log`
    (payload JSON), `run_id=NULL` para nivel orquestador.
  - Fallback: `logs/orchestrator.jsonl`, una línea JSON por evento.
- Tipos: `orchestrator_throttle`, `orchestrator_scale_up`, `trial_failed`,
  `resource_guard`.

### 3.4 Walk-forward

- `backtest.walkforward_runner.run_walk_forward(cfg, ...)` construye folds
  rolling o anchored (`anchored=True` ⇒ expanding window).
- Conversión `días → ms`: `int(days * 86_400_000)` (sin sorpresas por
  `timedelta`).
- Optimización opcional por fold (`--optimize_per_fold` + `--trials_per_fold`).
- Métricas agregadas vía `backtest.aggregator.aggregate_walk_forward_metrics`:
  `train_mean_total_return`, `test_mean_total_return`, `decay_test_vs_train_pct`,
  `train_test_correlation_total_return`, `test_best/worst_total_return`,
  `test_mean_sharpe`, `test_median_sharpe`, `per_fold_summary`.
- Salida: `walk_forward_report.md` + `fold_summary.csv`.

Detalles en [`WALKFORWARD.md`](WALKFORWARD.md).

### 3.5 Multi-symbol

- `backtest.multi_symbol.run_multi_symbol(cfg, ...)` corre la misma estrategia
  con los mismos parámetros sobre una canasta CSV.
- Cada símbolo: bankroll independiente (`initial_cash_per_symbol`), `run_id`
  separado, artefactos completos en `data/events/run_<id>/...` y
  `data/equity/run_<id>/equity.parquet`.
- Métricas agregadas: `mean_total_return`, `median_total_return`,
  `best_symbol`, `worst_symbol`, `dispersion_pct`, `per_symbol_summary`.
- Salida: `multi_symbol_report.md` + `per_symbol_summary.csv`.
- **Reservado**: `share_cash_pool=True` lanza
  `NotImplementedError("joint-pool multi-symbol is reserved for a future
  phase")`. La forma del flag se mantiene estable para no romper scripts.

Detalles en [`MULTI_SYMBOL.md`](MULTI_SYMBOL.md).

### 3.6 DuckDB reads (reportes)

- `backtest.plots._select_backend(run_id, data_root, db_path)` aplica la
  regla `auto`:

```python
if duckdb_reads.is_available() and duckdb_reads.has_equity_parquet(run_id, data_root):
    return "duckdb"
return "sqlite"
```

- DuckDB lee `data/equity/run_<id>/equity.parquet`; cae a
  `data/events/run_<id>/part-*.parquet` filtrando `equity IS NOT NULL` cuando
  no hay equity.parquet.
- Aggregations pesadas (`monthly_returns_aggregate`) se ejecutan en SQL/C++
  vectorizado en lugar de iterar bar a bar en Python.
- Salida byte-equivalente a SQLite. El backend resuelto se loguea por
  stderr: `[reports] run_id=<id> backend={duckdb|sqlite}`.
- `trial_objectives_from_parquet(study)` devuelve `None` mientras no exista
  `data/studies/<name>/trials.parquet`; los reportes caen al backend de
  metadatos correcto.

Detalles en [`REPORTS.md`](REPORTS.md).

### 3.7 Biblioteca de bots

- Estructura por entrada: `library/<kind>s/<name>/{manifest.yaml,
  strategy.py|indicator.py|tool.py, notes.md, presets/*.yaml,
  examples/, tests/}`.
- `library/workspace/` aloja drafts no auto-registrados.
- `manifest.yaml` declara `entry_point` (`module:Class` o
  `library.<kind>s.<name>.<file>:Symbol`), `default_params`, `search_space`,
  `data_requirements`, `tags`, `reference_only`.
- Auto-registro: `backtest.library.register_with_strategy_registry()` se
  llama de manera idempotente desde `backtest_cli.py main()`.
- `DataProvider` (`backtest/data_provider.py`): selector
  `parquet|sqlite|auto` vía `BACKTEST_DATA_BACKEND` o argumento explícito.
- Hooks a `registry.PARAMS_FROM_CLI_OVERRIDES` y
  `registry.SUGGEST_PARAMS_OVERRIDES` para personalizar mapeos CLI/Optuna
  por entrada.

Detalles, esquema completo y catálogo inicial en [`LIBRARY.md`](LIBRARY.md).

---

## 4. Directivas operativas revisadas

### DOs

- **DO** usar Parquet como fuente de verdad para klines. SQLite (`klines.db`)
  queda como espejo opcional para consumidores legacy.
- **DO** activar `--checkpoint_every_bars` (5000–10000) en runs con
  más de ~100k barras. El overhead es despreciable y el costo de un crash
  sin checkpoint es la corrida entera.
- **DO** correr `pytest tests/ -q` antes de cualquier publicación de bot;
  esto incluye los tests de paridad, biblioteca, walk-forward y multi-symbol.
- **DO** preferir `--metadata-backend pg` cuando hay >2 workers concurrentes
  optimizando el mismo study (Optuna en SQLite es un único writer efectivo).
- **DO** loguear el backend resuelto antes de auditar reportes
  (`[reports] run_id=<id> backend=...`).

### DON'Ts

- **DON'T** optimizar con `--executor serial` en máquinas multi-core; usa
  `joblib` o `ray`. Serial sólo está para reproducir runs históricos.
- **DON'T** mezclar metadatos PG con eventos SQLite (o viceversa). El
  `storage_facade` reconcilia, pero la auditoría se vuelve un dolor; usa
  el mismo backend de punta a punta.
- **DON'T** publicar bots con `validate` fallando o tests rojos; `publish`
  no bloquea por sí mismo si saltás validación, pero es trampa de pie.
- **DON'T** manipular `data/checkpoints/run_<id>/` a mano. La escritura es
  atómica (`.tmp`+`os.replace`); cualquier corrupción queda como inconsistencia
  silenciosa al resumir.
- **DON'T** asumir que `--share_cash_pool` funciona: hoy lanza
  `NotImplementedError`.

---

## 5. Tests del suite y qué cubren

| Archivo | Qué garantiza |
|---|---|
| `tests/test_phase0.py` | Layout `StoragePaths`, atomic write `tmp_then_rename`, idempotency keys canónicas, presencia de migraciones SQL, `AppConfig.from_env()`, `storage_facade.get_storage()` por backend. |
| `tests/test_engine_rs_parity.py` | Paridad numérica Python ↔ Rust (`math.isclose(1e-12, 1e-9)`) sobre 11 métricas canónicas con `SmaCrossStrategy` y 5000 barras sintéticas. |
| `tests/test_genericbt_core_shim.py` | Contrato del shim: `import` siempre OK, `is_rust_available()` devuelve bool, fallback Python si la wheel falta, dispatch correcto según `BACKTEST_ENGINE_KIND`. |
| `tests/test_streaming.py` | `iter_candles_arrow_batches` lee desde el lake por row-group y respeta `start_ts/end_ts`; el engine streaming produce los mismos resultados que el in-memory; iterador vacío no rompe. |
| `tests/test_checkpoint.py` | Roundtrip JSON, `latest_checkpoint_path` con múltiples archivos / dir vacío / dir inexistente, escritura del engine bajo umbral, **resume bit-equivalente** a single-shot, **no-regression** byte-idéntica cuando los flags están en `None`. |
| `tests/test_resume_runner.py` | `execute_and_persist_resumable` resuelve el último checkpoint y patchea `EngineConfig.resume_from_checkpoint`/`checkpoints_dir`. |
| `tests/test_orchestrator.py` | `map` serial, isolation joblib, emisión de `orchestrator_throttle` cuando ResourceGuard supera el cap, fallback automático cuando Ray no está disponible, `from_app_config` smoke. |
| `tests/test_optuna_pg.py` | `build_storage` cae a SQLite cuando no hay PG, construye URL `postgresql+psycopg://...?options=-csearch_path%3Doptuna%2Cpublic` cuando hay DSN, preserva query strings, `ensure_optuna_schema` (skip si no hay PG en CI). |
| `tests/test_walkforward_runner.py` | Construcción de ventanas rolling y anchored, `run_walk_forward` serial, signo del `decay_test_vs_train_pct`, `WalkForwardWindow` frozen. |
| `tests/test_multi_symbol.py` | Ejecución serial sobre canasta, agregación cross-symbol, `share_cash_pool=True` lanza `NotImplementedError`. |
| `tests/test_plots_parquet.py` | Resolución de equity desde Parquet vía DuckDB, selector `auto` que prioriza DuckDB cuando hay Parquet, fallback SQLite cuando no, `render_run_dashboard` por backend. |
| `tests/test_duckdb_reads.py` | Disponibilidad de DuckDB, conexión, roundtrip sintético de equity, `monthly_returns_aggregate`, fallback a `events` cuando no hay `equity.parquet`, `trial_objectives_from_parquet` retorna `None` sin parquet. |
| `tests/test_library.py` | Listado de entradas migradas + reference-only, instanciación con defaults, registro idempotente con `STRATEGY_REGISTRY`, `validate` detecta manifests rotos, scaffold + publish desde workspace, presets, refresh del índice, `DataProvider` (factory + Parquet round-trip + SQLite). |
| `tests/test_registry.py` | Estrategias core listables, `get_strategy` por nombre, `params_from_cli` por estrategia, suggest params Dorothy, restore de estado interno. |
| `tests/test_metrics.py` | Métricas canónicas (`max_drawdown`, `win_rate`, `profit_factor`, `calmar`, `sortino`) bajo edge cases. |
| `tests/test_broker.py` | `SpotBroker` ejecutando market orders. |
| `tests/test_pecunator_trend.py` | Heikin-Ashi + gates de `pecunator_trend`. |
| `tests/test_arch_incremental.py` | Resources, guards, gaps de minutos, batched inserts, scheduler, retry backoff bounded. |

---

## 6. Plataformas soportadas

- **Sistemas operativos** (CI matrix en `.github/workflows/ci.yml`):
  Linux x86_64, macOS x86_64/aarch64, Windows x64.
- **Python**: 3.11 y 3.13.
- **Wheels Rust** (`.github/workflows/wheels.yml`): manylinux x86_64 +
  aarch64, Windows x64 + x86, macOS x86_64 + aarch64. `abi3-py311` (un solo
  binario sirve para 3.11/3.12/3.13).
- **PostgreSQL** (`.github/workflows/pg-integration.yml`): smoke + CRUD sobre
  `meta.runs`/`meta.run_metrics`/`meta.checkpoints` contra `postgres:16-alpine`
  en service container. Local: Docker Desktop o instalación nativa
  (`chocolatey install postgresql16` en Windows).
- **Integridad nightly** (`.github/workflows/nightly-integrity.yml`):
  validación periódica del lake Parquet + checks adicionales.
