"""Data feed utilities backed by local SQLite klines (with optional Parquet cache)."""
import os
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

from db import iter_query_klines, query_klines


_MS_PER_DAY = 24 * 60 * 60 * 1000
_MS_PER_MONTH = 30 * _MS_PER_DAY


def _parquet_cache_enabled() -> bool:
    return os.getenv("BACKTEST_PARQUET_CACHE", "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Candle:
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

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "open_time": self.open_time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "close_time": self.close_time,
            "quote_asset_volume": self.quote_asset_volume,
            "num_trades": self.num_trades,
            "taker_buy_base": self.taker_buy_base,
            "taker_buy_quote": self.taker_buy_quote,
            "ignore_field": self.ignore_field,
        }


def load_candles(
    db_path: str,
    symbol: str,
    interval: str,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
) -> List[Candle]:
    rows: Optional[List[Tuple]] = None
    if _parquet_cache_enabled() and start_ts is not None and end_ts is not None:
        try:
            from backtest.data_cache import read_window  # local import to keep optional

            rows = read_window(symbol=symbol, interval=interval, start_ts=int(start_ts), end_ts=int(end_ts))
        except Exception:
            rows = None
    if rows is None:
        rows = query_klines(
            db_path,
            symbol=symbol,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
            limit=None,
        )
    return [
        Candle(
            symbol=r[0],
            interval=r[1],
            open_time=int(r[2]),
            open=float(r[3]),
            high=float(r[4]),
            low=float(r[5]),
            close=float(r[6]),
            volume=float(r[7]),
            close_time=int(r[8]) if r[8] is not None else None,
            quote_asset_volume=float(r[9]) if r[9] is not None else None,
            num_trades=int(r[10]),
            taker_buy_base=float(r[11]) if r[11] is not None else None,
            taker_buy_quote=float(r[12]) if r[12] is not None else None,
            ignore_field=str(r[13]) if r[13] is not None else None,
        )
        for r in rows
    ]


def candles_to_dicts(candles: Iterable[Candle]) -> List[Dict]:
    return [c.to_dict() for c in candles]


def _row_to_candle(row: Tuple) -> Candle:
    return Candle(
        symbol=row[0],
        interval=row[1],
        open_time=int(row[2]),
        open=float(row[3]),
        high=float(row[4]),
        low=float(row[5]),
        close=float(row[6]),
        volume=float(row[7]),
        close_time=int(row[8]) if row[8] is not None else None,
        quote_asset_volume=float(row[9]) if row[9] is not None else None,
        num_trades=int(row[10]),
        taker_buy_base=float(row[11]) if row[11] is not None else None,
        taker_buy_quote=float(row[12]) if row[12] is not None else None,
        ignore_field=str(row[13]) if row[13] is not None else None,
    )


def iter_candles(
    db_path: str,
    symbol: str,
    interval: str,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    fetch_size: int = 10000,
) -> Iterator[Candle]:
    """Stream candles one by one using a cursor (RAM bounded)."""
    for row in iter_query_klines(
        db_path,
        symbol=symbol,
        interval=interval,
        start_ts=start_ts,
        end_ts=end_ts,
        fetch_size=fetch_size,
    ):
        yield _row_to_candle(row)


def iter_candles_chunked(
    db_path: str,
    symbol: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    chunk_months: int = 1,
) -> Iterator[List[Candle]]:
    """Yield candles in temporal chunks (default monthly).

    Useful for orchestrators that want to run a chunked backtest (one engine
    invocation per chunk) without holding the full annual dataset in RAM.
    Returns a list per chunk; consumers may convert via `candles_to_dicts`.
    """
    if start_ts >= end_ts:
        return
    chunk_ms = max(1, int(chunk_months)) * _MS_PER_MONTH
    cursor = int(start_ts)
    while cursor < int(end_ts):
        chunk_end = min(int(end_ts), cursor + chunk_ms - 1)
        bucket = list(
            iter_candles(
                db_path,
                symbol=symbol,
                interval=interval,
                start_ts=cursor,
                end_ts=chunk_end,
            )
        )
        if bucket:
            yield bucket
        cursor = chunk_end + 1

