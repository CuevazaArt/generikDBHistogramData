"""Data feed utilities backed by local SQLite klines."""
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

from db import query_klines


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

