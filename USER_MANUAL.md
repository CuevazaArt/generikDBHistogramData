# Manual del Operador

Manual de operación post-rediseño. Voz directa, alta densidad. Cada flag y
comando aquí existe en `backtest_cli.py` o en los scripts del repo.

---

## 1. Visión general

El sistema corre simulaciones bar-a-bar sobre series OHLCV con dos motores
intercambiables: **Python** (referencia, siempre presente) o **Rust** opcional
vía `genericbt_core` (wheel `pyo3`/`maturin`). Los **klines** viven en Parquet
particionado por `symbol/interval/year/month` (con `klines.db` SQLite legacy
como espejo). Los **eventos** y la **curva de equity** por run se persisten en
Parquet append-only (`data/events/run_<id>/...`, `data/equity/run_<id>/equity.parquet`).
Los **metadatos** (runs, trials, métricas, audit) usan SQLite por defecto y
PostgreSQL opt-in vía `BACKTEST_METADATA_BACKEND=pg`. La biblioteca `library/`
empaqueta bots, indicadores y tools con manifest + presets + notas. Un
**orquestador paralelo** con `ResourceGuard` maneja Optuna en Ray/joblib/serial,
y los subcomandos `walk-forward` y `multi-symbol` extienden la evaluación.

---

## 2. Requisitos previos

- **Python 3.11+** (3.13 también soportado).
- `pip install -r requirements.txt` (incluye `pyarrow`, `duckdb`, `psycopg`,
  `optuna`, `matplotlib`, `pyyaml`).
- Opcional dev: `pip install -r requirements-dev.txt` (`maturin`, `ruff`,
  `mypy`, `pre-commit`).
- **Docker Desktop** opcional para PostgreSQL local (`infra/docker-compose.yml`).
  Alternativa nativa: `chocolatey install postgresql16` (Windows).
- **Rust toolchain** opcional (1.81+ vía [`rustup`](https://rustup.rs)) sólo
  para construir la wheel local con `maturin develop`. CI publica wheels
  prebuiltas; el operador no necesita Rust para correr el motor Python.

---

## 3. Inicio rápido

Receta mínima usando el corpus existente
(`data/klines/symbol=BTCUSDT/interval=1h/...`, espejo en `klines.db`):

```powershell
# 1. Activar venv y dependencias
python -m venv .venv ; .venv\Scripts\activate
pip install -r requirements.txt

# 2. (opcional) refrescar klines desde Binance
python cli.py --mode api --symbol BTCUSDT --interval 1h --start 2024-01-01 --end 2024-12-31 --db klines.db

# 3. Primer backtest
python backtest_cli.py --db klines.db run --strategy dorothy --symbol BTCUSDT --interval 1h

# 4. Primera optimización (joblib, 30 trials)
python backtest_cli.py --db klines.db optimize --strategy dorothy --symbol BTCUSDT --interval 1h --study quickstart --trials 30 --executor joblib

# 5. Primer walk-forward (90/30/30)
python backtest_cli.py --db klines.db walk-forward --strategy dorothy --symbol BTCUSDT --interval 1h --start_ts 1704067200000 --end_ts 1735603200000 --train_window_days 90 --test_window_days 30 --step_days 30 --output_dir reports/walkforward/quickstart

# 6. Reporte del último run
python backtest_cli.py --db klines.db plot --run_id 1 --output_dir reports
```

---

## 4. Subcomandos CLI

Todos los subcomandos cuelgan de `python backtest_cli.py [--db klines.db] <subcomando>`.

| Subcomando | Para qué sirve | Flags clave | Referencia |
|---|---|---|---|
| `run` | Un backtest single-symbol con persistencia de run + métricas + eventos. | `--strategy`, `--symbol`, `--interval`, `--start_ts/--end_ts`, `--initial_cash`, `--fee_rate`, `--slippage_bps`, `--heikin_ashi`, `--loop_seconds`, `--checkpoint_every_bars`, `--checkpoint_every_sim_seconds`, `--checkpoints_dir`, `--engine`, `--resume`, `--metadata-backend`, `--pg_dsn` | [CHECKPOINTING.md](docs/CHECKPOINTING.md), [RUST_CORE.md](docs/RUST_CORE.md) |
| `optimize` | Búsqueda Optuna sobre el espacio de parámetros de la estrategia. | `--study`, `--trials`, `--n_jobs`, `--executor {ray,joblib,serial}` (default `joblib`), `--ram_cap_pct`, `--cpu_cap_pct`, `--per_worker_ram_mb`, `--per_trial_timeout_sec`, `--timeout`, `--engine`, `--metadata-backend`, `--pg_dsn` | [POSTGRES.md](docs/POSTGRES.md) §7 |
| `sweet-spot` | Búsqueda en dos fases (gruesa + focal) con guard `adaptive_80`. | `--mode`, `--coarse_window_pct`, `--coarse_trials`, `--top_k`, `--objective_metric`, `--direction`, `--sampler`, `--seed`, `--guard_*`, `--executor`, `--ram_cap_pct`, `--cpu_cap_pct`, `--per_worker_ram_mb`, `--per_trial_timeout_sec` | [POSTGRES.md](docs/POSTGRES.md) §7 |
| `show` | Listar runs, mostrar resumen + eventos recientes de un run o top-trials de un study. | `--run_id`, `--limit`, `--study`, `--events_limit`, `--metadata-backend`, `--pg_dsn` | — |
| `plot` | Renderizar gráficas + Markdown integrado para un run o un study. | `--run_id`, `--study`, `--output_dir`, `--signal_bins`, `--metadata-backend`, `--pg_dsn` | [REPORTS.md](docs/REPORTS.md) |
| `cleanup` | Marcar runs colgados como `aborted`, opcionalmente purgar sus eventos. | `--purge_events` | — |
| `cache` | Materializar/verificar el cache columnar Parquet por mes para un símbolo/interval. | `action {materialize,verify}`, `--symbol`, `--interval`, `--start_ts`, `--end_ts`, `--cache_root`, `--overwrite` | — |
| `walk-forward` | Folds rodantes o anchored, opcionalmente con Optuna por fold. | `--strategy`, `--symbol`, `--interval`, `--start_ts/--end_ts`, `--train_window_days`, `--test_window_days`, `--step_days`, `--anchored`, `--optimize_per_fold`, `--trials_per_fold`, `--initial_cash`, `--fee_rate`, `--slippage_bps`, `--output_dir` | [WALKFORWARD.md](docs/WALKFORWARD.md) |
| `multi-symbol` | Misma estrategia sobre una canasta de símbolos. | `--symbols` (CSV), `--interval`, `--start_ts/--end_ts` (opcionales), `--initial_cash_per_symbol`, `--share_cash_pool` (reservado), `--fee_rate`, `--slippage_bps`, `--output_dir` | [MULTI_SYMBOL.md](docs/MULTI_SYMBOL.md) |
| `library` | Operar sobre `library/` (bots/indicadores/tools). | acciones: `list`, `show`, `new`, `publish`, `validate`, `notes`, `presets`, `refresh`, `import-aporte` (cada una con sus flags) | [LIBRARY.md](docs/LIBRARY.md) |
| `pg-init` | Aplicar migraciones SQL sobre PostgreSQL (idempotente). | `--dsn`, `--dry-run` | [POSTGRES.md](docs/POSTGRES.md) §2 |
| `migrate` | Migrar artefactos legacy (SQLite + Parquet) a PostgreSQL. | `--dsn`, `--from-sqlite`, `--data-root`, `--dry-run` | [POSTGRES.md](docs/POSTGRES.md) §3 |
| `menu` | Menú interactivo para los flujos más comunes. | — | — |

### Otros scripts auxiliares

Estos no son subcomandos de `backtest_cli.py` pero forman parte del flujo de datos:

| Script | Para qué sirve |
|---|---|
| `python cli.py --mode {api,alpha_api,zip} ...` | Descargar klines de Binance hacia `klines.db`. |
| `python scripts/backup_klines_to_parquet.py` | Exportar `klines.db` a `data/klines/symbol=*/interval=*/year=*/month=*/part-000.parquet`. |
| `python scripts/verify_klines_backup.py` | Verificar que el backup Parquet cubre las particiones esperadas. |
| `python scripts/inspect_klines_db.py` | Diagnóstico rápido del SQLite legacy. |
| `python scripts/pg_init.py --dsn $env:PG_DSN` | Equivalente standalone de `backtest_cli.py pg-init`. |
| `python scripts/migrate_to_pg.py --dsn $env:PG_DSN` | Equivalente standalone de `backtest_cli.py migrate`. |
| `scripts/pg_local_up.ps1` / `scripts/pg_local_down.ps1` | Levantar/detener PostgreSQL local vía Docker. |

---

## 5. Flags transversales (engine + storage)

Disponibles en `run`, `optimize`, `sweet-spot` (engine + storage) y en `show`,
`plot` (sólo storage):

| Flag | Valores | Efecto | Env equivalente |
|---|---|---|---|
| `--engine` | `python` (default), `rust` | Selecciona el motor. `rust` requiere `genericbt_core` importable; si falta, cae a `python` con un warning. | `BACKTEST_ENGINE_KIND` |
| `--metadata-backend` | `auto` (default), `sqlite`, `pg` | Backend para `meta.runs`, `meta.run_metrics`, `meta.trial_runs`, `ops.*`. `auto` elige `pg` si `PG_DSN` está seteado, si no `sqlite`. | `BACKTEST_METADATA_BACKEND` |
| `--pg_dsn` | URL `postgresql://...` | DSN PostgreSQL. Sobrescribe el env. | `PG_DSN` |
| `--resume` | `<run_id>` (entero) | Reanuda un run interrumpido desde su último checkpoint. Si no hay checkpoint, arranca desde cero con aviso. | `BACKTEST_RESUME_RUN_ID` |

---

## 6. Checkpointing y resume

- **Activación**: `--checkpoint_every_bars N` (cada N barras procesadas) y/o
  `--checkpoint_every_sim_seconds S` (cada S segundos de tiempo simulado). Si
  ambos están seteados, gana el primero que se cumpla. Cuando los dos son
  `None` y no se pidió `--resume`, la ruta rápida es **byte-idéntica** al
  motor pre-Fase-2 (test `test_engine_no_regression_when_disabled`).
- **Ubicación**: `data/checkpoints/run_<id>/cp_<sim_ts>.json` (override con
  `--checkpoints_dir`). Honra `BACKTEST_DATA_ROOT`. Escritura atómica
  `.tmp`+`os.replace`; ningún lector ve archivos parciales.
- **Resume**: `python backtest_cli.py run ... --resume <run_id>`. El CLI
  setea `BACKTEST_RESUME_RUN_ID` y el dispatcher elige
  `execute_and_persist_resumable`, que resuelve `latest_checkpoint_path()`,
  patchea `EngineConfig.resume_from_checkpoint`, restaura `broker.state` +
  `strategy.import_state(...)` y reanuda exactamente en
  `candle_offset + 1`. Imprime `[resume] run_id=<id> from <path>`.
- **Bit-equivalencia**: `tests/test_checkpoint.py::test_engine_resume_continues`
  garantiza igualdad estricta entre un run single-shot y uno con
  cortes/resume; el campo `engine_kind` en el snapshot deja la puerta abierta
  a rechazar checkpoints cross-engine si la semántica divergiera.
- **Auditoría**: con backend `pg`, se inserta una fila *best-effort* en
  `ops.audit_log` con `event_type='resume'`. Detalles completos en
  [`docs/CHECKPOINTING.md`](docs/CHECKPOINTING.md).

---

## 7. Orquestador paralelo

Activable en `optimize` y `sweet-spot` vía `--executor {ray,joblib,serial}`.
El orquestador (`backtest.orchestrator.Orchestrator`) corre cada trial en un
subprocess aislado con crash-isolation y RAM cap.

| Flag | Default | Efecto |
|---|---|---|
| `--executor` | `joblib` (en `optimize` y `sweet-spot`) | `serial` mantiene el loop legado en un solo proceso. `ray` cae a `joblib` con warning si Ray no es importable. |
| `--n_jobs` | `cpu_count()` | Concurrencia inicial; el guard la mueve entre 1 y `recommend_n_jobs('adaptive_80')`. |
| `--ram_cap_pct` | `80.0` | Cap RAM del `ResourceGuard`. Presión sostenida → `orchestrator_throttle` y la concurrencia baja a la mitad. |
| `--cpu_cap_pct` | `80.0` | Cap CPU análogo. |
| `--per_worker_ram_mb` | `None` | Cap duro por subprocess (rlimit en Linux/macOS, watchdog psutil en Windows). Excederlo termina el worker como `MemoryError`. |
| `--per_trial_timeout_sec` | `None` | Wall-clock por trial; al expirar, el subprocess se mata limpio y el trial queda `failed`. |

Headroom sostenido emite `orchestrator_scale_up` (concurrencia +1 hasta el
techo). Cualquier crash/timeout aislado emite `trial_failed` y deja un
`FailureResult` en su slot, sin matar la batch. Los eventos van a
`ops.resource_events` + `ops.audit_log` cuando el backend metadata es `pg`,
o a `logs/orchestrator.jsonl` (una línea JSON por evento) en caso contrario.
Receta completa en [`docs/POSTGRES.md`](docs/POSTGRES.md) §7.

---

## 8. PostgreSQL local

Por defecto el backend de metadatos es **SQLite** (`klines.db`). PG es opt-in:

```powershell
# 1. Levantar PostgreSQL local en Docker (puerto 5433)
scripts/pg_local_up.ps1

# 2. Configurar DSN
$env:PG_DSN = "postgresql://genericbt:genericbt@127.0.0.1:5433/genericbt"
$env:BACKTEST_METADATA_BACKEND = "pg"

# 3. Aplicar migraciones (idempotente)
python backtest_cli.py pg-init --dsn $env:PG_DSN

# 4. (opcional) migrar artefactos legacy
python backtest_cli.py migrate --dsn $env:PG_DSN --from-sqlite klines.db --data-root data --dry-run
python backtest_cli.py migrate --dsn $env:PG_DSN --from-sqlite klines.db --data-root data

# 5. Apagar el contenedor sin perder datos
scripts/pg_local_down.ps1
```

Los schemas creados son `meta` (runs, trials, métricas, checkpoints), `ops`
(resource_events, audit_log) y `optuna` (lo crea Optuna en su primer
`create_study`). Detalles, schema completo y resolución de la URL Optuna en
[`docs/POSTGRES.md`](docs/POSTGRES.md).

---

## 9. Biblioteca de bots

`library/` agrupa **bots**, **indicadores** y **tools** en directorios
autocontenidos con `manifest.yaml`, `notes.md` y `presets/*.yaml`. El
auto-registro (`backtest.library.register_with_strategy_registry`) las hace
disponibles para `--strategy <nombre_lib>` sin tocar `STRATEGY_REGISTRY`.

```bash
python backtest_cli.py library list [--kind bot|indicator|tool] [--tag <tag>] [--workspace]
python backtest_cli.py library show <name>
python backtest_cli.py library new <name> [--kind bot|indicator|tool]
python backtest_cli.py library validate <name> [--workspace]
python backtest_cli.py library publish <name> [--kind bot|indicator|tool]
python backtest_cli.py library notes <name>
python backtest_cli.py library presets <name>
python backtest_cli.py library refresh
python backtest_cli.py library import-aporte <name>
```

`workspace/` aloja drafts no auto-registrados; `publish` los mueve a la
carpeta canónica y regenera `_index.json`. Catálogo completo, esquema del
manifest y uso del `DataProvider` en [`docs/LIBRARY.md`](docs/LIBRARY.md).

---

## 10. Walk-forward y multi-symbol

**Walk-forward** construye ventanas en milisegundos:
`train_window_ms = train_window_days * 86_400_000` (idem `test`/`step`). En
modo rolling cada fold avanza `step` días; con `--anchored` train arranca
siempre en `start_ts` y crece (expanding window). Con `--optimize_per_fold`
corre Optuna en train y aplica `best_params` en test (`--trials_per_fold`,
default 30). Salida en `--output_dir`:

| Archivo | Contenido |
|---|---|
| `walk_forward_report.md` | Resumen humano: configuración, agregados, tabla de folds, veredicto sobre `decay_test_vs_train_pct`. |
| `fold_summary.csv` | Detalle por fold: ventanas, `train_run_id`, `test_run_id`, returns, sharpe. |

**Multi-symbol** corre la misma estrategia con los mismos parámetros sobre
una canasta CSV (`--symbols BTCUSDT,XRPUSDT,ETHUSDT`). Cada símbolo recibe
su propio bankroll (`--initial_cash_per_symbol`) y un `run_id` separado.
Salida en `--output_dir`:

| Archivo | Contenido |
|---|---|
| `multi_symbol_report.md` | Configuración, mejor/peor símbolo, dispersión, tabla por símbolo. |
| `per_symbol_summary.csv` | `symbol`, `run_id`, `total_return`, `sharpe`, `win_rate`, `num_trades`, `final_equity`. |

**Límite actual**: `--share_cash_pool` está aceptado en la CLI pero dispara
`NotImplementedError("joint-pool multi-symbol is reserved for a future
phase")`. La forma del flag se conserva estable para no romper scripts.
Detalles en [`docs/WALKFORWARD.md`](docs/WALKFORWARD.md) y
[`docs/MULTI_SYMBOL.md`](docs/MULTI_SYMBOL.md).

---

## 11. Reportes (DuckDB sobre Parquet, fallback SQLite)

`render_run_dashboard` y `build_unified_report` usan la regla `auto`:

- **DuckDB** cuando el módulo `duckdb` está instalado **y** existe alguno
  de `data/equity/run_<id>/equity.parquet` o `data/events/run_<id>/part-*.parquet`.
- **SQLite** como fallback automático si el run sólo vive en `klines.db`
  legacy o si DuckDB no está disponible.

El backend resuelto se loguea por stderr (`[reports] run_id=<id> backend=...`).
Las salidas (PNG, CSV, JSON, MD) son **byte-equivalentes** entre ambos
backends; la diferencia es latencia y RAM en runs con millones de eventos.
Detalles, fuerce manual de backend y ejemplo de aggregation pushed-down en
[`docs/REPORTS.md`](docs/REPORTS.md).

---

## 12. Variables de entorno

| Variable | Default | Efecto |
|---|---|---|
| `PG_DSN` | `<sin default>` | DSN PostgreSQL. Acepta `postgresql://...` o `postgresql+psycopg://...`. |
| `BACKTEST_METADATA_BACKEND` | `sqlite` si `PG_DSN` vacío, si no `pg` | Selecciona el backend del facade (`pg` o `sqlite`). |
| `BACKTEST_DATA_ROOT` | `data` | Raíz del lake Parquet (klines/events/equity/checkpoints). |
| `BACKTEST_SQLITE_PATH` | `klines.db` | Ruta al SQLite legacy cuando se usa ese backend. |
| `BACKTEST_ENGINE_KIND` | `python` | `python` o `rust`. `rust` requiere `genericbt_core` importable. |
| `BACKTEST_EVENTS_MODE` | `lite` | `full` / `lite` / `minimal`. Controla la verbosidad del sink de eventos. |
| `BACKTEST_RESUME_RUN_ID` | `<unset>` | Lo setea el flag `--resume`. Activa la ruta `execute_and_persist_resumable`. |
| `BACKTEST_EVENTS_PARQUET` | `0` | Si `1/true/yes/on` y `events_mode=full`, escribe sidecar `events.parquet` por run en `reports/entregables/runs/run_<id>/`. |
| `BACKTEST_DATA_BACKEND` | `auto` | `parquet` / `sqlite`. Selecciona el `DataProvider` para la biblioteca. |
| `BACKTEST_GUARD_CPU_CAP_PCT` | `80.0` | Cap CPU del `ResourceGuard` cuando se construye `from_env()`. |
| `BACKTEST_GUARD_RAM_CAP_PCT` | `80.0` | Cap RAM análogo. |
| `BACKTEST_GUARD_SAMPLE_SEC` | `5.0` | Período de muestreo del guard. |
| `BACKTEST_GUARD_HIGH_WATERMARK_WINDOWS` | `3` | Ventanas consecutivas para disparar throttle. |
| `BACKTEST_GUARD_RECOVER_WINDOWS` | `3` | Ventanas consecutivas para escalar +1. |

Las primeras seis las consume `AppConfig.from_env()` (en `backtest/config.py`);
las últimas cinco las consume `ResourceGuardConfig.from_env()`.

---

## 13. Rust core (opcional)

`pip install maturin && maturin develop --manifest-path crates/genericbt-core/Cargo.toml --release`
construye la wheel local y registra `genericbt_core` en el venv activo. Si la
wheel no está disponible, el shim `genericbt_core/__init__.py` cae al engine
Python sin cambios funcionales: la única diferencia es throughput y huella de
RAM. CI publica wheels prebuiltas (manylinux x86_64/aarch64, Windows
x64/x86, macOS x86_64/aarch64; `abi3-py311`) vía
`.github/workflows/wheels.yml`. Paridad numérica auditada por
`tests/test_engine_rs_parity.py` con `math.isclose(rel_tol=1e-12,
abs_tol=1e-9)` sobre las 11 métricas canónicas. Detalles en
[`docs/RUST_CORE.md`](docs/RUST_CORE.md).

---

## 14. Recetas comunes

> Notación: `# powershell` o `# bash` indica el shell objetivo. Para PG_DSN
> reemplaza el valor por el tuyo. Las recetas usan el corpus existente
> `BTCUSDT 1h`; ajusta a tus símbolos.

### 14.1 Backtest single-symbol con checkpoint cada 5000 barras

```powershell
# powershell
python backtest_cli.py --db klines.db run --strategy dorothy --symbol BTCUSDT --interval 1h --start_ts 1704067200000 --end_ts 1735603200000 --checkpoint_every_bars 5000
```

Genera `data/checkpoints/run_<id>/cp_<sim_ts>.json` cada 5000 barras
procesadas. Sin overhead si los flags de checkpoint están en `None`.

### 14.2 Resume después de crash

```powershell
# powershell
python backtest_cli.py --db klines.db run --strategy dorothy --symbol BTCUSDT --interval 1h --start_ts 1704067200000 --end_ts 1735603200000 --checkpoint_every_bars 5000 --resume <run_id>
```

El runner imprime `[resume] run_id=<id> from <path>` y reanuda en
`candle_offset+1`. Si no hay checkpoint, arranca desde cero con aviso.

### 14.3 Optimización joblib 4 workers, RAM 70%

```powershell
# powershell
python backtest_cli.py --db klines.db optimize --strategy dorothy --symbol BTCUSDT --interval 1h --study dorothy_70 --trials 64 --n_jobs 4 --executor joblib --ram_cap_pct 70 --cpu_cap_pct 70 --per_worker_ram_mb 1024 --per_trial_timeout_sec 600
```

Cuatro subprocesses aislados, cap duro 1 GB por trial, timeout 10 min;
`ResourceGuard` baja la concurrencia bajo presión sostenida.

### 14.4 Walk-forward con Optuna por fold

```powershell
# powershell
python backtest_cli.py --db klines.db walk-forward --strategy dorothy --symbol BTCUSDT --interval 1h --start_ts 1704067200000 --end_ts 1735603200000 --train_window_days 90 --test_window_days 30 --step_days 30 --optimize_per_fold --trials_per_fold 20 --output_dir reports/walkforward/dorothy_btc_optuna
```

Cada fold corre `optimize_strategy` (20 trials) sobre train y reusa
`best_params` para evaluar test. Reporte en `walk_forward_report.md` +
`fold_summary.csv`.

### 14.5 Multi-symbol 3 cryptos con engine Rust

```powershell
# powershell
$env:BACKTEST_ENGINE_KIND = "rust"
python backtest_cli.py --db klines.db multi-symbol --strategy dorothy --symbols BTCUSDT,XRPUSDT,SOLUSDT --interval 1h --output_dir reports/multi_symbol/dorothy_basket
```

`--engine rust` también está disponible directamente en `run`/`optimize`/`sweet-spot`.
Si la wheel no está, cae a Python con warning; el reporte sale igual.

### 14.6 Migración SQLite → PG

```powershell
# powershell
$env:PG_DSN = "postgresql://genericbt:genericbt@127.0.0.1:5433/genericbt"
python backtest_cli.py pg-init --dsn $env:PG_DSN
python backtest_cli.py migrate --dsn $env:PG_DSN --from-sqlite klines.db --data-root data --dry-run
python backtest_cli.py migrate --dsn $env:PG_DSN --from-sqlite klines.db --data-root data
```

Aplica migraciones, valida el backup Parquet (`data/klines/_manifest.json`),
porta `bt_runs/bt_metrics/bt_trials/bt_events` a `meta.*` + Parquet por run.

### 14.7 Render dashboard de un run vía DuckDB

```powershell
# powershell
python backtest_cli.py --db klines.db plot --run_id 42 --output_dir reports/runs/run_42
```

Si existen `data/equity/run_42/equity.parquet` o `data/events/run_42/part-*.parquet`,
el dashboard usa DuckDB; si no, cae a SQLite. Loguea
`[reports] run_id=42 backend=...` por stderr.

### 14.8 Publicar un bot draft a la biblioteca

```bash
# bash
python backtest_cli.py library new mi_bot --kind bot
# editar library/workspace/mi_bot/strategy.py | manifest.yaml | notes.md | presets/default.yaml
python backtest_cli.py library validate mi_bot --workspace
python backtest_cli.py library publish mi_bot
python backtest_cli.py run --strategy mi_bot --symbol BTCUSDT --interval 1h
```

Equivalente PowerShell: idéntico salvo prefijo `python`. `validate` falla si
el manifest está mal formado o si la clase no instancia con defaults; corrige
antes de `publish`.
