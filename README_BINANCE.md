# Binance histogram downloader (local, plug-and-play)

Este pequeño proyecto permite descargar datos de klines (histogramas) desde Binance y guardarlos en una base de datos SQLite local para que otros servicios los consuman fácilmente.

Uso básico (API):

```
python cli.py --mode api --symbol BTCUSDT --interval 1m --start 2021-01-01 --end 2021-01-02 --db /path/to/klines.db
```

Uso por ZIP (descarga mensual desde data.binance.vision):

```
python cli.py --mode zip --symbol BTCUSDT --interval 1m --year 2021 --month 1 --db /path/to/klines.db
```

Notas:
- El DB resultante contiene la tabla `klines` con la clave primaria `(symbol, interval, open_time)` para evitar duplicados.
- Otros servicios pueden leer los datos importando `reader.query`:

```
from reader import query
rows = query("klines.db", "BTCUSDT", "1m", start_ts=1609459200000)
```

- Interfaz de terminal interactiva:

```
python terminal_ui.py
```

- Instalar dependencias: `pip install -r requirements.txt`.

## Servicio HTTP local

Ejecuta el servicio local:

```
python service.py
```

Por defecto el servicio local escucha en el puerto `8004`.

Luego consulta datos con:

```
curl "http://127.0.0.1:8004/klines?db=klines.db&symbol=BTCUSDT&interval=1h&start_ts=1609459200000&limit=10"
```

La ruta de salud es:

```
curl http://127.0.0.1:8004/health
```

Exportar datos via HTTP:

```
curl "http://127.0.0.1:8004/export?db=klines.db&symbol=BTCUSDT&interval=1h&format=csv" -o btc_1h.csv
```

## Backtesting + Optimización (MVP)

Se añadió un engine local para evaluar estrategias sobre los datos de `klines` ya almacenados.

Ejecutar un backtest:

```
python backtest_cli.py --db klines.db run --symbol BTCUSDT --interval 1h --fast 10 --slow 30
```

Optimizar parámetros (Optuna + SQLite, paralelo CPU):

```
python backtest_cli.py --db klines.db optimize --symbol BTCUSDT --interval 1h --study sma_opt --trials 50 --n_jobs 4
```

Ver resultados:

```
python backtest_cli.py --db klines.db show --run_id 1
python backtest_cli.py --db klines.db show --study sma_opt
```

Generar gráficas y exports:

```
python backtest_cli.py --db klines.db plot --run_id 1 --output_dir reports
python backtest_cli.py --db klines.db plot --study sma_opt --output_dir reports
```

Menú interactivo de terminal:

```
python backtest_cli.py --db klines.db menu
```
