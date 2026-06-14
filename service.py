"""Expose local SQLite kline data over HTTP with FastAPI."""
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from typing import Optional, List
from pydantic import BaseModel
from db import query_klines
from fastapi.responses import StreamingResponse
import io
import csv
import json

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
    close_time: int | None
    quote_asset_volume: float | None
    num_trades: int
    taker_buy_base: float | None
    taker_buy_quote: float | None
    ignore_field: str | None


@app.get("/health")
def health() -> JSONResponse:
    return JSONResponse({"status": "ok"})


@app.get("/klines", response_model=List[KlineRow])
def get_klines(
    db: str = Query("klines.db", description="Ruta local al archivo sqlite"),
    symbol: str = Query(..., description="Símbolo de mercado, por ejemplo BTCUSDT"),
    interval: str = Query(..., description="Intervalo de kline, por ejemplo 1m o 1h"),
    start_ts: int | None = Query(None, description="Timestamp de inicio en ms"),
    end_ts: int | None = Query(None, description="Timestamp de fin en ms"),
    limit: int | None = Query(None, description="Número máximo de filas"),
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


@app.get("/export")
def export_klines(
    db: str = Query("klines.db", description="Ruta local al archivo sqlite"),
    symbol: str = Query(..., description="Símbolo de mercado, por ejemplo BTCUSDT"),
    interval: str = Query(..., description="Intervalo de kline, por ejemplo 1m o 1h"),
    start_ts: int | None = Query(None, description="Timestamp de inicio en ms"),
    end_ts: int | None = Query(None, description="Timestamp de fin en ms"),
    format: str = Query("csv", description="Formato de export: csv o json"),
) -> StreamingResponse:
    """Export rows matching query in `csv` or `json` format as a downloadable stream."""
    try:
        rows = query_klines(db, symbol, interval, start_ts=start_ts, end_ts=end_ts, limit=None)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    keys = ["symbol","interval","open_time","open","high","low","close","volume","close_time","quote_asset_volume","num_trades","taker_buy_base","taker_buy_quote","ignore_field"]
    if format.lower() == "json":
        def gen_json():
            yield "["
            first = True
            for r in rows:
                obj = {k: v for k, v in zip(keys, r)}
                if not first:
                    yield ",\n"
                else:
                    first = False
                yield json.dumps(obj, ensure_ascii=False)
            yield "]"

        return StreamingResponse(gen_json(), media_type="application/json")

    # default CSV
    def gen_csv():
        buff = io.StringIO()
        writer = csv.writer(buff)
        writer.writerow(keys)
        yield buff.getvalue()
        buff.seek(0)
        buff.truncate(0)
        for r in rows:
            writer.writerow(r)
            yield buff.getvalue()
            buff.seek(0)
            buff.truncate(0)

    return StreamingResponse(gen_csv(), media_type="text/csv", headers={"Content-Disposition": f"attachment; filename=export_{symbol}_{interval}.{format.lower()}"})


if __name__ == "__main__":
    import uvicorn

    # Default local port changed to 8004
    uvicorn.run(app, host="127.0.0.1", port=8004)
