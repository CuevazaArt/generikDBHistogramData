# Fase 0: PostgreSQL local + lake Parquet

Esta guia cubre el bootstrap de PostgreSQL para el framework de backtesting,
las migraciones y la herramienta de migracion desde el SQLite legacy.

Nada de lo descrito aqui es obligatorio en Fase 0: el backend SQLite sigue
funcionando exactamente como antes. PG entra por *feature flag*
(`BACKTEST_METADATA_BACKEND=pg`), y los consumidores actuales que aun no
opten in conservan su comportamiento.

---

## 1. Levantar PostgreSQL localmente

Camino recomendado: Docker Desktop + `docker compose`.

```powershell
# desde la raiz del repo
scripts/pg_local_up.ps1
# o, equivalentemente,
docker compose -f infra/docker-compose.yml up -d
```

El servicio `genericbt-postgres`:

- Imagen `postgres:16-alpine`.
- Puerto host **5433** mapeado a 5432 del contenedor (evita choques con
  instalaciones nativas que ya usan 5432).
- Volumen persistente `pg_data`.
- Healthcheck via `pg_isready` cada 5 s.
- `restart: unless-stopped`.

Las credenciales por defecto (`genericbt/genericbt/genericbt`) viven en
`infra/.env.example`. Copia ese archivo a `infra/.env` y modificalo si
necesitas algo distinto; `docker compose` lo lee automaticamente.

Si Docker no esta disponible, `scripts/pg_local_up.ps1` imprime las
instrucciones para instalar PostgreSQL 16 nativo via chocolatey:

```powershell
choco install postgresql16 --params '/Password:genericbt /Port:5433'
psql -U postgres -c "CREATE ROLE genericbt LOGIN PASSWORD 'genericbt';"
psql -U postgres -c "CREATE DATABASE genericbt OWNER genericbt;"
```

Para parar el contenedor (sin perder datos):

```powershell
scripts/pg_local_down.ps1
```

Pasa `-RemoveVolume` para borrar tambien el volumen `pg_data`.

---

## 2. Aplicar migraciones

Las migraciones viven en `backtest/migrations/sql/V*.sql` y se aplican en
orden lexicografico. Cada archivo corre dentro de su propia transaccion y
se registra en `meta.schema_migrations`.

```powershell
$env:PG_DSN = "postgresql://genericbt:genericbt@127.0.0.1:5433/genericbt"
python scripts/pg_init.py --dsn $env:PG_DSN
```

`pg_init.py` imprime las versiones aplicadas y la version final. Es
idempotente: re-ejecutar despues de un cambio aplica solo lo nuevo.

Para inspeccionar sin tocar la DB:

```powershell
python scripts/pg_init.py --dsn $env:PG_DSN --dry-run
```

Migraciones presentes en Fase 0:

| Archivo | Contenido |
|---|---|
| `V0001__schema_meta.sql` | Schemas `meta` y `ops`, tablas `meta.runs`, `meta.run_metrics`, `meta.studies`, `meta.trial_runs`, `meta.trial_metrics`, `meta.checkpoints`, `ops.resource_events`, `ops.audit_log` |
| `V0002__indexes.sql` | Indices secundarios para el orquestador y los dashboards (status/symbol/strategy, indices por trial, ops.*, etc.) |

Las tablas Optuna (schema `optuna`) las crea Optuna por si mismo la primera
vez que se llama `optuna.create_study(storage="postgresql+psycopg://...")`.

---

## 3. Migrar runs/eventos legacy

`scripts/migrate_to_pg.py` porta `bt_runs`, `bt_metrics`, `bt_trials` y
`bt_events` desde `klines.db` hacia PG + Parquet. Despues del *borron y
cuenta nueva* reciente no deberia haber filas que migrar; el script lo
detecta y reporta `0 runs, 0 trials`. Si volvieran a aparecer, pasos:

```powershell
# 1) Vista previa: no escribe nada
python scripts/migrate_to_pg.py --dsn $env:PG_DSN --dry-run

# 2) Ejecutar la migracion real
python scripts/migrate_to_pg.py --dsn $env:PG_DSN
```

Comportamiento:

1. Aplica las migraciones (`apply_migrations`).
2. Verifica el backup Parquet de klines: lee `data/klines/_manifest.json`
   y comprueba que cada particion listada existe. Aborta antes de tocar
   nada si falta alguna.
3. Para cada `bt_run`: calcula la `idempotency_key`, hace `INSERT` en
   `meta.runs`, escribe `data/events/run_<new_id>/part-000.parquet` con
   los `bt_events` y upserta `meta.run_metrics`.
4. Para cada `bt_trial`: crea la fila en `meta.studies` si hace falta
   y hace `INSERT` en `meta.trial_runs` matcheado por `(study_name,
   optuna_trial_num)`.
5. Reporte final con conteos y bytes Parquet escritos.

Flags:

- `--dsn <url>` (o env `PG_DSN`)
- `--from-sqlite klines.db`
- `--data-root data`
- `--dry-run`

---

## 4. Variables de entorno relevantes

| Variable | Propósito | Default |
|---|---|---|
| `PG_DSN` | DSN de PostgreSQL. Acepta `postgresql://...` o `postgresql+psycopg://...` | (sin default) |
| `BACKTEST_METADATA_BACKEND` | `pg` o `sqlite`. Selecciona el backend del facade | `sqlite` si `PG_DSN` esta vacio, sino `pg` |
| `BACKTEST_DATA_ROOT` | Raiz del lake Parquet (klines/events/equity/checkpoints) | `data` |
| `BACKTEST_SQLITE_PATH` | Ruta al SQLite legacy cuando se usa ese backend | `klines.db` |
| `BACKTEST_ENGINE_KIND` | `python` o `rust` (Fase 1) | `python` |
| `BACKTEST_EVENTS_MODE` | `full` / `lite` / `minimal` | `lite` |

`infra/.env.example` documenta los valores recomendados para desarrollo
local.

---

## 5. Layout de archivos

```
data/
  klines/symbol=<SYM>/interval=<INT>/year=<YYYY>/month=<MM>/part-000.parquet
  klines/_manifest.json
  events/run_<id>/part-<seq>.parquet            # append-only
  equity/run_<id>/equity.parquet
  checkpoints/run_<id>/cp_<sim_ts>.parquet
  derived/<name>/...
reports/                                         # SIN CAMBIO
  entregables/runs/run_<id>/...
  entregables/studies/<study>/...
```

`backtest.storage_paths.StoragePaths` es la fuente de verdad de estas
rutas. Toda escritura pasa por `tmp_then_rename` (escritura a `.tmp`
seguida de `os.replace`), de modo que los lectores nunca ven un archivo
parcial.

---

## 6. Estado de la integracion con la CLI

En Fase 0 los nuevos componentes se exponen exclusivamente como scripts
standalone bajo `scripts/`. La integracion con `backtest_cli.py`
(`pg-init`, `migrate`, `--engine`, `--resume`, `--pg_dsn`) es una tarea de
seguimiento dedicada para evitar choques con otros cambios en curso en
ese archivo.

Mientras tanto, codigo nuevo que quiera el backend PG puede hacer:

```python
from backtest.config import AppConfig
from backtest.storage_facade import get_storage

backend = get_storage(AppConfig.from_env())
backend.create_run(...)
```

---

## 7. Orchestrator y Optuna respaldado por PostgreSQL

Fase 3 introduce un orquestador real (`backtest.orchestrator.Orchestrator`)
que reemplaza el pool de hilos interno de Optuna por procesos aislados con
crash-isolation, throttling adaptativo y backend Optuna en PostgreSQL.

### 7.1 Storage de Optuna

`backtest/optuna_storage.py::build_storage(study_name, app_config)` resuelve
la URL que Optuna usa internamente:

- Si `AppConfig.metadata_backend == 'pg'` y `PG_DSN` esta seteado, devuelve
  `postgresql+psycopg://...?options=-csearch_path%3Doptuna%2Cpublic`. Las
  tablas de Optuna (`studies`, `trials`, etc.) caen en el schema `optuna`
  para no mezclarse con `meta.*` ni `ops.*`. Antes de devolver la URL,
  `ensure_optuna_schema(dsn)` aplica `CREATE SCHEMA IF NOT EXISTS optuna`.
- En caso contrario devuelve `sqlite:///<path>` (comportamiento legado).

Con esto, multiples workers pueden compartir el mismo estudio sin la
contension de un unico SQLite-writer.

### 7.2 Modos de ejecucion del comando `optimize`

`backtest_cli.py optimize` acepta los nuevos flags:

```powershell
python backtest_cli.py optimize `
  --symbol XRPUSDT --interval 1h --strategy dorothy `
  --study fase3_demo --trials 32 --n_jobs 4 `
  --executor joblib --ram_cap_pct 70 --cpu_cap_pct 70 `
  --per_worker_ram_mb 1024 --per_trial_timeout_sec 600
```

| Flag | Proposito |
|---|---|
| `--executor` | `joblib` (default) usa procesos. `ray` usa Ray si esta importable; si no, cae a joblib con warning. `serial` mantiene el loop legado en un solo proceso. |
| `--n_jobs` | Concurrencia inicial. El orquestador la ajusta entre 1 y `recommend_n_jobs(adaptive_80)`. |
| `--ram_cap_pct` / `--cpu_cap_pct` | Umbrales del `ResourceGuard` (default 80%). Si el sistema los excede de forma sostenida, el orquestador reduce la concurrencia. |
| `--per_worker_ram_mb` | Cap duro de RAM por worker (rlimit en Linux/macOS, watchdog psutil en Windows). Un worker que excede el cap termina con `MemoryError`. |
| `--per_trial_timeout_sec` | Cap wall-clock por trial. Al expirar el subprocess se termina limpio. |

`sweet-spot` acepta los mismos cinco flags y los aplica tanto a la fase
gruesa como a la fase focal.

Cuando `--executor != serial`, la CLI llama internamente a
`optimize_strategy_parallel(...)`, que construye el `Orchestrator`,
resuelve la URL de Optuna (PG o SQLite segun `AppConfig`) y dispatcha cada
trial en su propio subprocess via `worker_isolation.spawn_isolated_worker`.
Con `--executor serial` se preserva la ruta legada a `optimize_strategy`.

### 7.3 ResourceGuard adaptive_80

El orquestador construye internamente un `ResourceGuard` con la
configuracion `adaptive_80` (target 80% CPU/RAM, histeresis de 3 ventanas
por defecto). Cada vez que se va a despachar una nueva ola de trials:

1. Toma un snapshot via `psutil.cpu_percent` + `psutil.virtual_memory`.
2. Si la presion sostenida supera el cap, halve los workers activos y emite
   `orchestrator_throttle` con el snapshot.
3. Si hay headroom durante varias ventanas, escala +1 hasta el techo dado
   por `recommend_n_jobs('adaptive_80')` y emite `orchestrator_scale_up`.
4. Cualquier crash o timeout aislado emite `trial_failed` y deja un
   `FailureResult` en la posicion correspondiente del resultado, sin tirar
   el resto de la batch.

### 7.4 Eventos de auditoria

Los eventos del orquestador caen en uno de dos destinos:

- **PostgreSQL** (cuando `metadata_backend == 'pg'` y la conexion
  funciona): cada evento se inserta en `ops.resource_events` (snapshot
  estructurado) y en `ops.audit_log` (payload JSON), ambos con
  `run_id = NULL` para eventos de nivel orquestador.
- **Fallback JSONL**: cuando PG no esta disponible, se appendea a
  `logs/orchestrator.jsonl` (path configurable via
  `OrchestratorConfig.log_path`). Una linea JSON por evento, con campos
  `ts`, `event_type`, `snapshot`, `run_id`.

Tipos de eventos emitidos:

| `event_type` | Cuando |
|---|---|
| `orchestrator_throttle` | Concurrencia baja a la mitad por presion sostenida. |
| `orchestrator_scale_up` | Concurrencia sube en 1 por headroom sostenido. |
| `trial_failed` | Un trial subprocess termino en error/timeout/OOM. |
| `resource_guard` | Eventos forwardeados del propio `ResourceGuard` (trigger/recovery). |

### 7.5 API programatica

```python
from backtest.config import AppConfig
from backtest.optimize import optimize_strategy_parallel
from backtest.engine import EngineConfig
from backtest.registry import get_strategy

study = optimize_strategy_parallel(
    db_path="klines.db",
    study_name="fase3_demo",
    strategy_cls=get_strategy("dorothy"),
    base_config=EngineConfig(...),
    trials=64,
    n_jobs=4,
    executor="joblib",
    app_config=AppConfig.from_env(),
    ram_cap_pct=70.0,
    per_worker_ram_mb=1024,
)
print(study.best_value, study.best_params)
```
