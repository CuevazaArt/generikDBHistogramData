# Reportes: DuckDB sobre Parquet con fallback a SQLite

La Fase 5 del rediseno porta la capa de reportes (`backtest/plots.py` y
`backtest/sweet_spot_report.py`) para que las graficas y los Markdown
integrados se generen leyendo Parquet via DuckDB cuando los artefactos
ya estan persistidos en el data lake. La salida visible al usuario
(PNG, CSV, JSON, MD) es identica a la del path historico de SQLite:
mismos nombres de archivo, mismas columnas, misma estructura de
secciones. La unica diferencia practica es la latencia para runs con
millones de eventos.

## Artefacto generico de dataset (prepare/verify)

Antes de correr reportes o backtests pesados, puede prepararse una ventana de
datos reutilizable para cualquier bot con `dataset prepare` y validarse con
`dataset verify`.

```powershell
python backtest_cli.py --db klines.db dataset prepare --symbol XRPUSDT --interval 1s --start_ts 1735689600000 --end_ts 1767225599000 --name xrp_2025_1s
python backtest_cli.py --db klines.db dataset verify --manifest reports/entregables/datasets/xrp_2025_1s/manifest.json
```

Los artefactos quedan en `reports/entregables/datasets/<name>/` y su
`manifest.json` deja trazabilidad de:
- integridad de la ventana (`row_count`, `gap_count`, `gaps`);
- reproducibilidad (`reproducibility.git`);
- archivos preparados (`prepared_data.files`), usando cache Parquet cuando esta
  disponible o snapshot JSONL de respaldo.

## Briefing pre-run (RUN_BRIEFING)

Antes de ejecutar una corrida pesada o comparativa, el proyecto debe emitir
`RUN_BRIEFING.md` y `run_briefing.json` en la carpeta del estudio. El briefing
documenta todo lo que puede impactar el desempeno: bot, params, gates,
accesorios, malla, motor, recursos y commit git.

Los strict runs en `scripts/run_xrpusdt_2024_dorothy_strict.py` lo generan
automaticamente al crear `output_dir`. Para otros flujos usar
`backtest.run_briefing.write_run_briefing`.

## Cuando se usa DuckDB y cuando SQLite

El selector vive en `backtest/plots.py` y aplica la regla del backend
"auto":

```python
def _select_backend(run_id, data_root, db_path):
    if duckdb_reads.is_available() and duckdb_reads.has_equity_parquet(run_id, data_root):
        return "duckdb"
    return "sqlite"
```

- DuckDB se elige cuando el modulo `duckdb` esta instalado **y** existe
  al menos uno de:
  - `data/equity/run_<id>/equity.parquet`
  - `data/events/run_<id>/part-*.parquet`
- SQLite es el fallback automatico: se usa cuando el run vive
  exclusivamente en la base SQLite legacy (sin Parquet escrito por
  Fase 2) o cuando DuckDB no esta disponible en el entorno.

El backend resuelto se loguea por stderr en cada render:

```
[reports] run_id=42 backend=duckdb
[reports] run_id=17 backend=sqlite
```

## Que archivos consume DuckDB

`backtest.duckdb_reads` solo lee del data lake; nunca escribe. Las rutas
las construye `backtest.storage_paths.StoragePaths` con `os.path.join`,
asi que funcionan tanto en Linux como en Windows.

| Lectura | Ruta esperada |
|---|---|
| Curva de equity | `data/equity/run_<id>/equity.parquet` |
| Fallback para equity | `data/events/run_<id>/part-*.parquet` (filtra `equity IS NOT NULL`) |
| Eventos completos / fills | `data/events/run_<id>/part-*.parquet` |

Trials de Optuna **no** se leen desde Parquet: viven en PostgreSQL o
SQLite. `trial_objectives_from_parquet(study)` devuelve `None` mientras
no exista `data/studies/<name>/trials.parquet`, y los reportes caen
automaticamente al backend de metadatos correcto.

## Como forzar un backend especifico

`render_run_dashboard` y `build_unified_report` siempre se quedan con
"auto" salvo que el llamador indique otra cosa. Los flags utiles son:

```python
from backtest.plots import render_run_dashboard

# Forzar Parquet: requiere artefactos en data/ y duckdb instalado.
render_run_dashboard(
    output_dir="reports/x",
    run_id=42,
    backend="duckdb",
    data_root="data",
)

# Forzar SQLite: requiere db_path apuntando al klines.db legacy.
render_run_dashboard(
    output_dir="reports/x",
    run_id=42,
    backend="sqlite",
    db_path="klines.db",
)

# Default recomendado.
render_run_dashboard(
    output_dir="reports/x",
    run_id=42,
    backend="auto",
    db_path="klines.db",  # se ignora si gana DuckDB
)
```

`db_path` sigue siendo necesario en "auto" para que el reporte
integrado pueda cargar metricas y descriptor desde la DB cuando los
datos de eventos vienen de Parquet.

## Aggregations pesadas: DuckDB lo hace en SQL

`monthly_returns_aggregate(run_id)` empuja la reduccion mensual al
motor de DuckDB en vez de iterar en Python:

```sql
WITH samples AS (
  SELECT seq, event_time, equity
  FROM read_parquet('data/equity/run_<id>/equity.parquet')
  WHERE equity IS NOT NULL AND event_time IS NOT NULL
  ORDER BY seq ASC
)
SELECT
  strftime(date_trunc('month', make_timestamp(event_time * 1000)), '%Y-%m') AS month,
  last(equity) - first(equity) AS pnl,
  CASE WHEN first(equity) = 0 THEN 0.0
       ELSE (last(equity) - first(equity)) / first(equity) END AS return_pct
FROM samples
GROUP BY 1
ORDER BY 1;
```

Sobre runs de varios millones de eventos, este patron evita materializar
toda la curva en Python y ejecuta la agregacion en C++ vectorizado.

## Por que importa

- En el path legacy `plot_monthly_return_heatmap` itera bar a bar en
  Python sobre la lista completa de filas que SQLite devuelve. Para un
  run anual a 1 segundo (alrededor de 31 millones de barras), eso copia
  la curva entera a la RAM del proceso de reporte y bloquea el GIL.
- En el path nuevo, DuckDB hace pruning de columnas, lee solo
  `seq/event_time/equity`, y ejecuta la agregacion sin salir del
  motor C++.
- Las graficas finales (`run_<id>_*.png`) y el Markdown integrado
  (`run_<id>_integrated_report.md`) salen byte-equivalentes al output
  de SQLite siempre que los datos persistidos sean los mismos, asi que
  ningun consumidor downstream necesita adaptaciones.

## Que sigue migrandose

Algunos consumidores aun usan el loop Python para iterar eventos:

- `plot_fill_activity_heatmap` recorre todos los eventos en Python para
  construir la grilla `(weekday, hour)`. Una version `aggregate` en
  DuckDB usando `extract('isodow', ...)` y `extract('hour', ...)`
  reduciria la carga a un `SELECT ... GROUP BY` puro.
- `plot_signal_histograms` puede pre-agregar los `seq` por
  `(event_type, side)` antes de delegar a matplotlib.

Estos son optimizaciones futuras: el resto de la integracion ya esta
lista y el output coincide con el de SQLite.
