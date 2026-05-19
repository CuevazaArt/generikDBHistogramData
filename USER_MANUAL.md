# Manual de Usuario — generikDBHistogramData

Este manual explica cómo instalar, iniciar y usar el servicio local de descarga y consulta de datos de histogramas (klines) desde Binance. Incluye ejemplos para backtesters, optimizadores de parámetros y bots de trading.

**Contenido**
- Instalación rápida
- Iniciar el servicio HTTP
- Interfaz de terminal (UI)
- Uso del CLI para descargas
- Catálogo de endpoints HTTP
- Esquema de la base de datos
- Ejemplos de consumo (Backtester / Optimizador / Bot)
- Logs y auditoría
- Buenas prácticas y límites de uso

---

## Instalación rápida

1. Clona el repositorio y sitúate en la carpeta del proyecto.

2. Crea un entorno virtual (opcional) e instala dependencias:

```bash
python -m venv .venv
source .venv/bin/activate   # o .\\.venv\\Scripts\\activate en Windows
pip install -r requirements.txt
```

3. Archivos clave:
- [binance_hist_downloader.py](binance_hist_downloader.py): funciones para descargar via API o ZIP mensual.
- [db.py](db.py): inicialización y consultas en SQLite.
- [cli.py](cli.py): utilidad de línea de comandos para descargas masivas.
- [service.py](service.py): servidor HTTP local (FastAPI) para exponer datos.
- [terminal_ui.py](terminal_ui.py): interfaz interactiva en terminal.

---

## Iniciar el servicio HTTP

Puedes ejecutar el servicio local directamente (útil para desarrollo):

```bash
python service.py
```

O con `uvicorn` (recomendado para despliegue local controlado):

```bash
uvicorn service:app --host 127.0.0.1 --port 8000 --reload
```

El servicio quedará disponible en `http://127.0.0.1:8000`.

---

## Interfaz de terminal (UI)

Arranca la interfaz interactiva para inspeccionar la DB y lanzar descargas:

```bash
python terminal_ui.py
```

Características:
- Menú sencillo para inspección, consultas y descargas (API / ZIP mensual).
- Opción de dar un paso atrás durante el seteo (presiona `b`).
- Muestra un resumen descriptivo antes de iniciar la descarga.
- Barra de progreso con % (usa `tqdm`).
- Resumen final con metadatos: filas insertadas, tiempo y estimados.
- Registra eventos en `logs/terminal_ui.log` (archivo rotatorio).

---

## Uso del CLI para descargas

Ejemplo descarga vía API:

```bash
python cli.py --mode api --symbol BTCUSDT --interval 1h --start 2021-01-01 --end 2021-01-02 --db klines.db
```

Ejemplo import ZIP mensual:

```bash
python cli.py --mode zip --symbol BTCUSDT --interval 1m --year 2021 --month 01 --db klines.db
```

---

## Catálogo de endpoints HTTP

Base URL: `http://127.0.0.1:8000`

- `GET /health`
  - Descripción: chequeo de salud simple.
  - Respuesta: `{"status":"ok"}`

- `GET /klines`
  - Descripción: obtiene filas de klines desde la DB SQLite local.
  - Parámetros (query):
    - `db` (string, opcional): ruta al archivo sqlite (por defecto `klines.db`).
    - `symbol` (string, requerido): p.ej. `BTCUSDT`.
    - `interval` (string, requerido): p.ej. `1m`, `1h`.
    - `start_ts` (int, opcional): timestamp en ms.
    - `end_ts` (int, opcional): timestamp en ms.
    - `limit` (int, opcional): número máximo de filas.
  - Respuesta: lista de objetos con schema (ver abajo).

Ejemplo curl:

```bash
curl "http://127.0.0.1:8000/klines?db=klines.db&symbol=BTCUSDT&interval=1h&start_ts=1609459200000&limit=100"
```

---

## Esquema de la base de datos

Tabla: `klines`

- `symbol` TEXT
- `interval` TEXT
- `open_time` INTEGER (ms)
- `open` REAL
- `high` REAL
- `low` REAL
- `close` REAL
- `volume` REAL
- `close_time` INTEGER (ms)
- `quote_asset_volume` REAL
- `num_trades` INTEGER
- `taker_buy_base` REAL
- `taker_buy_quote` REAL
- `ignore_field` TEXT

PK: `(symbol, interval, open_time)` — evita duplicados.

---

## Ejemplos de consumo

1) Backtester (descargar rango histórico y generar serie OHLC):

```python
import requests
import pandas as pd

resp = requests.get('http://127.0.0.1:8000/klines', params={
    'db': 'klines.db', 'symbol': 'BTCUSDT', 'interval': '1m', 'start_ts': 1609459200000
})
data = resp.json()
df = pd.DataFrame(data)
df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
df.set_index('open_time', inplace=True)

# ahora `df` contiene la serie OHLC para backtesting
```

2) Optimizador de parámetros (requiere ventanas y métricas):

- El optimizador típicamente solicita un bloque histórico (start/end) con `limit` grande y calcula resultados fuera de línea.
- Use el endpoint `/klines` para obtener la serie completa y luego pase las columnas `open, high, low, close, volume` al optimizador.

3) Bot de trading en producción (modo local):

- Para decisiones en tiempo real, un bot puede recuperar las últimas N filas:

```python
params = {'db':'klines.db','symbol':'BTCUSDT','interval':'1m','limit':200}
resp = requests.get('http://127.0.0.1:8000/klines', params=params)
rows = resp.json()
```

- Combine esto con un pequeño proceso que llame periódicamente al endpoint local y evalúe señales.

Notas para integradores:
- Los backtesters y optimizadores pueden trabajar con una copia del archivo `klines.db` si desean aislar ejecución.
- Para alta concurrencia o uso remoto, considere exponer el servicio detrás de un proxy y asegurar acceso.

---

## Logs y auditoría

- La interfaz de terminal escribe logs rotativos en `logs/terminal_ui.log`.
- `service.py` (FastAPI) muestra logs por consola (uvicorn) — reconfigure uvicorn para logs persistentes si lo necesitas.

---

## Buenas prácticas y límites de uso

- Respeta los límites de uso de las APIs públicas de Binance: Evita enviar muchas peticiones simultáneas; usa `limit` razonables y descarga por rangos.
- Para grandes volúmenes históricos prefiere los ZIP mensuales (`data.binance.vision`) en lugar de paginar millones de llamadas a la REST API.
- Añade retrasos exponenciales si recibes `429 Too Many Requests`.

---

## Extensiones recomendadas

- Añadir autenticación y control de acceso si el servicio se expone en red.
- Exportar logs en JSON para ingestión por sistemas de observabilidad.
- Añadir endpoints especializados para: `latest`, `ohlc_window(N)`, `aggregate(range, interval)` para optimizadores.

---

Para cualquier ajuste o añadir endpoints concretos para tu flujo de backtesting/trading, dime qué formatos y filtros necesitas y los implemento.
