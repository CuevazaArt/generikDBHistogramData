"""Expose local SQLite kline data over HTTP with FastAPI."""
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from typing import Optional, List
from pydantic import BaseModel
from db import query_klines

app = FastAPI(
    title="Binance Kline Local Service",
    description="Servicio local para consultar datos de Binance almacenados en SQLite.",
    version="0.1.0",
)


class KlineRow(BaseModel):
    symbol: str
    interval: str
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: Optional[int]
    quote_asset_volume: Optional[float]
    num_trades: int
    taker_buy_base: Optional[float]
    taker_buy_quote: Optional[float]
    ignore_field: Optional[str]


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/klines", response_model=List[KlineRow])
def get_klines(
    db: str = Query("klines.db", description="Ruta local al archivo sqlite"),
    symbol: str = Query(..., description="Símbolo de mercado, por ejemplo BTCUSDT"),
    interval: str = Query(..., description="Intervalo de kline, por ejemplo 1m o 1h"),
    start_ts: Optional[int] = Query(None, description="Timestamp de inicio en ms"),
    end_ts: Optional[int] = Query(None, description="Timestamp de fin en ms"),
    limit: Optional[int] = Query(None, description="Número máximo de filas"),
) -> List[KlineRow]:
    try:
        rows = query_klines(db, symbol, interval, start_ts=start_ts, end_ts=end_ts, limit=limit)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return [KlineRow(
        symbol=r[0],
        interval=r[1],
        open_time=r[2],
        open=r[3],
        high=r[4],
        low=r[5],
        close=r[6],
        volume=r[7],
        close_time=r[8],
        quote_asset_volume=r[9],
        num_trades=r[10],
        taker_buy_base=r[11],
        taker_buy_quote=r[12],
        ignore_field=r[13],
    ) for r in rows]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
