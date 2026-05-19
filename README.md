# generikDBHistogramData

Proyecto para descargar y almacenar datos de histogramas (klines) de Binance en SQLite, con una API local para consumo inmediato.

## Documentación

- `README_BINANCE.md`: instrucciones detalladas de uso, instalación y ejemplos.

## Contenido

- `binance_hist_downloader.py`: downloader de klines vía API y ZIP de Binance.
- `cli.py`: CLI para descargar datos y guardarlos en SQLite.
- `db.py`: helper SQLite para crear el esquema y consultar datos.
- `service.py`: servicio HTTP local con FastAPI.
- `reader.py`: cliente local para consultar datos desde otros servicios.
- `backtest_cli.py`: interfaz de terminal para backtesting y optimización.
- `backtest/`: módulos de engine, métricas, eventos, optimización y gráficas.
- `requirements.txt`: dependencias necesarias.
