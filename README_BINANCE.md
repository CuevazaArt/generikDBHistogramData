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
