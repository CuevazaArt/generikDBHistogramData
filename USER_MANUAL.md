# Manual de Usuario — generikDBHistogramData

Este manual explica cómo instalar, iniciar y usar el servicio local de descarga y consulta de datos de histogramas (klines) desde Binance. Incluye ejemplos para backtesters, optimizadores de parámetros y bots de trading.

**Contenido**
- Instalación rápida
- Iniciar el servicio HTTP
- Interfaz de terminal (UI)
- Uso del CLI para descargas
- Backtesting y optimización en terminal
- Catálogo de endpoints HTTP
- Esquema de la base de datos
- Esquema de backtesting en DB
- Interpretación de resultados (métricas y artefactos)
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
uvicorn service:app --host 127.0.0.1 --port 8004 --reload
```

Por defecto el servicio escucha en `http://127.0.0.1:8004`.

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

## Backtesting y optimización en terminal

La herramienta `backtest_cli.py` permite ejecutar evaluación histórica, optimizar parámetros y revisar resultados persistidos.

Estrategias disponibles en esta versión:
- `dorothy` (adaptada de `aportes/dorothy.py` para backtesting local).
- `sma_cross` (estrategia base de medias móviles).

1) Ejecutar un backtest simple:

```bash
python backtest_cli.py --db klines.db run --strategy dorothy --symbol BTCUSDT --interval 1h --profit_factor 0.05 --margin_drop_factor 0.004 --stop_loss_pct 0.10 --size_pct 0.7
```

2) Optimizar parámetros con Optuna:

```bash
python backtest_cli.py --db klines.db optimize --strategy dorothy --symbol BTCUSDT --interval 1h --study dorothy_opt --trials 50 --n_jobs 4
```

Parámetros principales de `dorothy`:
- `profit_factor`: porcentaje objetivo de toma de ganancia.
- `margin_drop_factor`: margen adicional de caída para activar recompra.
- `quote_order_qty_usdt`: nocional por operación (recomendado 8).
- `min_order_notional`: mínimo nocional permitido (6 USDT).
- `max_order_notional`: máximo nocional permitido (10 USDT).
- `max_active_orders`: máximo de órdenes/targets activos simultáneos (200).

Notas de modelado actual de Dorothy:
- No utiliza stop loss; el cierre se hace por activación de límite de venta.
- Se registra drawdown en métricas y eventos para evaluación de riesgo.
- La optimización se centra en combinaciones de `profit_factor` y `margin_drop_factor`.

3) Revisar resultados:

```bash
python backtest_cli.py --db klines.db show --run_id 3
python backtest_cli.py --db klines.db show --study eval_opt
```

4) Generar gráficas y exportaciones:

```bash
python backtest_cli.py --db klines.db plot --run_id 3 --output_dir reports
python backtest_cli.py --db klines.db plot --study eval_opt --output_dir reports
```

5) Menú interactivo:

```bash
python backtest_cli.py --db klines.db menu
```

---

## Catálogo de endpoints HTTP

Base URL: `http://127.0.0.1:8004`

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
curl "http://127.0.0.1:8004/klines?db=klines.db&symbol=BTCUSDT&interval=1h&start_ts=1609459200000&limit=100"
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

## Esquema de backtesting en DB

Además de `klines`, el sistema guarda resultados de evaluación y optimización:

- `bt_runs`: metadatos de cada corrida (estrategia, rango, costos, estado, timestamps).
- `bt_events`: bitácora paso a paso de eventos (`hold`, `fill`, `order_rejected`) con `seq`, `event_time`, estado de cartera y `payload_json`.
- `bt_metrics`: métricas agregadas por corrida/trial (`total_return`, `max_drawdown`, etc.).
- `bt_trials`: resultados de optimización por trial (parámetros, objetivo, estado, duración).
- `bt_trial_metrics`: métricas detalladas por trial para análisis comparativo.

---

## Interpretación de resultados (métricas y artefactos)

Esta sección explica exactamente los elementos que viste al validar localmente.

### Backtest ejecutado

- `run_id=3`: identificador único en DB para esa corrida.
- `BTCUSDT 1h`: símbolo e intervalo usados como entrada histórica.
- `fast=10`, `slow=30`: parámetros de la estrategia (SMA rápida/lenta).

### Resultado principal

- `final_equity: 9581.84`
  - Capital final de la cuenta simulada al cierre de la corrida.
  - Incluye caja (`cash`) + valor de posición abierta a precio de mercado final.

- `total_return: -4.18%`
  - Retorno total sobre el capital inicial.
  - Fórmula: `(final_equity - initial_cash) / initial_cash`.

- `max_drawdown: 36.79%`
  - Mayor caída porcentual desde un pico de equity hasta un valle posterior.
  - Mide riesgo de pérdida temporal durante la estrategia.

- `sharpe: 0.0102`
  - Relación retorno/riesgo de la curva de equity.
  - Cerca de 0 implica señal débil ajustada por volatilidad.

- `win_rate: 34.64%`
  - Proporción de trades cerrados con PnL positivo.
  - No implica por sí solo rentabilidad; depende también del tamaño medio de ganancia/pérdida.

- `profit_factor: 1.2179`
  - `ganancia_bruta / pérdida_bruta`.
  - >1 significa que la estrategia ganó más de lo que perdió en agregado de trades.

- `num_trades: 179`
  - Número de trades contabilizados para métricas (cierres evaluados en PnL).

### Optimización ejecutada

- `study=eval_opt`: nombre lógico del experimento de Optuna.
- `3 trials`: cantidad de configuraciones evaluadas.
- `n_jobs=2`: ejecución paralela local en CPU con 2 workers.
- `best_params: fast=38, slow=72`: mejor set encontrado en ese experimento.
- `best_value: 1.123024`: valor objetivo máximo reportado por Optuna (en este MVP, `total_return`).

### Persistencia confirmada en DB

- Runs registrados/completados:
  - Quedan almacenados en `bt_runs` con estado `completed/failed`.
- Trials guardados:
  - Se registran en `bt_trials` y métricas extendidas en `bt_trial_metrics`.
- Eventos paso a paso:
  - Cada barra queda auditada en `bt_events` para reconstruir decisiones y estado del portafolio.

### Gráficas y exports generados en `reports/`

- `reports/run_3_equity.png`
  - Curva de equity a lo largo del tiempo (o secuencia de barras).

- `reports/run_3_drawdown.png`
  - Serie de drawdown instantáneo respecto al máximo acumulado.

- `reports/run_3_returns_hist.png`
  - Histograma de retornos por paso (distribución de retornos).

- `reports/run_3_metrics.json`
  - Snapshot serializado de métricas agregadas de la corrida.

- `reports/run_3_equity.csv`
  - Serie exportada (`seq`, `event_time`, `equity`) para análisis externo.

- `reports/run_<id>_report.json`
  - Reporte descriptivo del run con:
    - símbolo, estrategia e intervalo,
    - timestamps de inicio/fin de configuración,
    - primera/última vela efectiva procesada (ms + ISO UTC),
    - estado del run y cantidad de eventos.

- `reports/study_eval_opt_trials.png`
  - Evolución del objetivo por número de trial dentro del estudio.

---

## Ejemplos de consumo

1) Backtester (descargar rango histórico y generar serie OHLC):

```python
import requests
import pandas as pd

resp = requests.get('http://127.0.0.1:8004/klines', params={
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
resp = requests.get('http://127.0.0.1:8004/klines', params=params)
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
