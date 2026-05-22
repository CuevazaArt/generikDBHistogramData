"""Data feed utilities backed by local SQLite klines (with optional Parquet cache)."""
import glob
import json
import os
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple

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


def _glob_partition_files(parquet_root: str, symbol: str, interval: str) -> List[str]:
    """Return Parquet part files for (symbol, interval), ordered chronologically.

    Layout follows :class:`backtest.storage_paths.StoragePaths.klines_partition`:

        <parquet_root>/symbol=<S>/interval=<I>/year=YYYY/month=MM/part-000.parquet

    A best-effort consult of ``<parquet_root>/_manifest.json`` is used when
    available so we benefit from the migration tool's deterministic ordering;
    otherwise we fall back to ``glob.glob`` + lexicographic sort (the
    ``year=YYYY/month=MM`` naming sorts correctly as strings).
    """
    root = os.fspath(parquet_root)
    manifest_path = os.path.join(root, "_manifest.json")
    rel_files: List[str] = []
    if os.path.isfile(manifest_path):
        try:
            with open(manifest_path, "r", encoding="utf-8") as fh:
                manifest = json.load(fh)
        except (OSError, ValueError):
            manifest = None
        if isinstance(manifest, dict):
            for series in manifest.get("series") or []:
                if (
                    str(series.get("symbol") or "") != symbol
                    or str(series.get("interval") or "") != interval
                ):
                    continue
                for part in series.get("partitions") or []:
                    rel = part.get("path")
                    if rel:
                        rel_files.append(os.path.join(root, str(rel)))
                break
    if not rel_files:
        pattern = os.path.join(
            root,
            f"symbol={symbol}",
            f"interval={interval}",
            "year=*",
            "month=*",
            "part-*.parquet",
        )
        rel_files = sorted(glob.glob(pattern))
    # Filter to existing paths; the manifest may reference partitions that
    # were pruned offline.
    return [p for p in rel_files if os.path.isfile(p)]


def iter_candles_arrow_batches(
    parquet_root: str,
    symbol: str,
    interval: str,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    batch_size: int = 65536,
) -> Iterator[List[Dict[str, Any]]]:
    """Yield candle batches read from the Parquet kline lake.

    The lake layout mirrors :class:`backtest.storage_paths.StoragePaths`:

        ``<parquet_root>/symbol=<S>/interval=<I>/year=YYYY/month=MM/part-000.parquet``

    Each yielded element is a ``list[dict]`` of up to ``batch_size`` candles.
    The ``[start_ts, end_ts]`` window is inclusive at both ends; when both
    bounds are ``None`` the entire series is emitted.

    Falls back to the SQLite iterator (``iter_candles``) when pyarrow is
    unavailable; the fallback uses ``BACKTEST_SQLITE_PATH`` env (defaulting
    to ``klines.db``) so callers that do not pre-materialise Parquet still
    work in tests.
    """
    try:
        import pyarrow.parquet as pq  # type: ignore[import-not-found]
    except ImportError:
        # Fallback: pull rows from SQLite and chunk into batches manually.
        db_path = os.getenv("BACKTEST_SQLITE_PATH", "klines.db")
        bucket: List[Dict[str, Any]] = []
        for candle in iter_candles(
            db_path,
            symbol=symbol,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
            fetch_size=batch_size,
        ):
            bucket.append(candle.to_dict())
            if len(bucket) >= batch_size:
                yield bucket
                bucket = []
        if bucket:
            yield bucket
        return

    paths = _glob_partition_files(parquet_root, symbol, interval)
    if not paths:
        return

    lo = int(start_ts) if start_ts is not None else None
    hi = int(end_ts) if end_ts is not None else None

    for path in paths:
        try:
            pf = pq.ParquetFile(path)
        except (OSError, ValueError):
            continue
        for arrow_batch in pf.iter_batches(batch_size=batch_size):
            rows = arrow_batch.to_pylist()
            if lo is None and hi is None:
                if rows:
                    yield rows
                continue
            kept: List[Dict[str, Any]] = []
            for row in rows:
                ts = row.get("open_time")
                if ts is None:
                    continue
                ts_i = int(ts)
                if lo is not None and ts_i < lo:
                    continue
                if hi is not None and ts_i > hi:
                    continue
                kept.append(row)
            if kept:
                yield kept

