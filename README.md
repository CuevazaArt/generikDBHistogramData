# generikDBHistogramData

## Directivas Operativas (obligatorias)

Antes de cualquier descarga, backtest u optimizacion:

1. Leer este `README.md` completo primero.
2. Mantener la app por debajo de `90%` CPU y `90%` RAM sostenidos.
3. Para cargas pesadas (`1s` anual), ejecutar por bloques (mensual/trimestral), no en una sola corrida monolitica.
4. No ejecutar varias corridas pesadas contra la misma base SQLite.
5. Todo entregable debe quedar en carpeta dedicada dentro de `reports/entregables/`.

## Vision General

Proyecto para:

- descargar klines de Binance (API/ZIP),
- almacenarlos localmente,
- exponerlos por API local,
- correr backtests y optimizaciones con persistencia de runs/trials/reportes.

## Instalacion Rapida

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
pip install -r requirements.txt
```

## Componentes Principales

- `binance_hist_downloader.py`: descarga historicos.
- `cli.py`: CLI de descarga/importacion.
- `service.py`: API HTTP local (FastAPI).
- `terminal_ui.py`: interfaz de terminal para datos.
- `backtest_cli.py`: CLI principal de backtesting/optimizacion/reportes.
- `backtest/`: motor, estrategias, optimize, sweet-spot, reportes.

## Descargador de Datos

Ejemplos:

```bash
python cli.py --mode api --symbol BTCUSDT --interval 1m --start 2021-01-01 --end 2021-01-02 --db klines.db
python cli.py --mode zip --symbol BTCUSDT --interval 1m --year 2024 --month 01 --db klines.db
```

Buenas practicas:

- Preferir ZIP mensual para historicos largos.
- Validar cobertura (`count`, `min(open_time)`, `max(open_time)`) antes de backtest.
- Reintentar con backoff cuando Binance limite solicitudes.

## Tester de Backtesting

### Comandos base

```bash
# menu interactivo
python backtest_cli.py --db klines.db menu

# run
python backtest_cli.py --db klines.db run --strategy dorothy --symbol XRPUSDT --interval 1s

# optimize
python backtest_cli.py --db klines.db optimize --strategy dorothy --symbol XRPUSDT --interval 1s --study dorothy_opt --trials 30

# mostrar resultados
python backtest_cli.py --db klines.db show --run_id 54
python backtest_cli.py --db klines.db show --study dorothy_opt

# generar reportes
python backtest_cli.py --db klines.db plot --run_id 54 --output_dir reports
python backtest_cli.py --db klines.db plot --study dorothy_opt --output_dir reports
```

### Modos robustos para cargas exigentes

- `sweet-spot`: busqueda en dos fases para evitar saturacion.
- `events_mode`: usar `lite`/`minimal` para datasets gigantes.
- `cleanup`: marcar corridas colgadas y limpiar eventos abortados.
- guardia de recursos: usar limites CPU/RAM y backoff para throttling.

## Convencion Unica de Entregables

Toda salida se normaliza bajo:

`reports/entregables/`

Estructura:

- `reports/entregables/runs/run_<id>/`
- `reports/entregables/studies/study_<name>/`
- `reports/entregables/strict/<strict_name>/`
- `reports/entregables/legacy/`
- `reports/entregables/INDEX.md`

Cada carpeta de run/study/strict debe incluir `MANIFEST.md`.

## Guardias Anti-Desbordamiento

El proyecto incorpora guardias dinamicos (`backtest/guards.py`) con:

- umbral CPU/RAM configurable,
- deteccion de presion sostenida (histéresis),
- reduccion de concurrencia sugerida,
- backoff y eventos de trigger/recovery para auditoria.

Ajuste por variables de entorno:

- `BACKTEST_GUARD_CPU_CAP_PCT`
- `BACKTEST_GUARD_RAM_CAP_PCT`
- `BACKTEST_GUARD_SAMPLE_SEC`
- `BACKTEST_GUARD_HIGH_WATERMARK_WINDOWS`
- `BACKTEST_GUARD_RECOVER_WINDOWS`

## Hoja de Ruta Resumida

1. Migrar persistencia masiva de eventos a formato columnar (Parquet/DuckDB).
2. Separar almacenamiento de trials/eventos para reducir contencion SQLite.
3. Mejorar procesamiento por bloques y consolidacion incremental.
4. Reforzar observabilidad de runs y orquestacion de colas de trabajo.

## Notas de Documentacion

Este `README.md` es la fuente canonica.
Los documentos historicos se mantienen solo como referencia y deben considerarse archivados.
