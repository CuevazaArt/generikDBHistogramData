"""Optional columnar Parquet cache for kline datasets.

Materializes `klines` SQLite reads into Parquet files partitioned by
`symbol/interval/yyyymm`. Loading from Parquet is much faster than SQLite
for large windows and keeps RAM bounded via Arrow columnar scans.

`pyarrow` is an optional dependency. If unavailable, the cache silently
becomes a no-op and callers fall back to `db.query_klines`.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from db import iter_query_klines

try:
    import pyarrow as pa  # type: ignore[import-not-found]
    import pyarrow.parquet as pq  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dependency
    pa = None  # type: ignore[assignment]
    pq = None  # type: ignore[assignment]


CACHE_ROOT_DEFAULT = os.path.join("reports", "cache", "parquet")


def is_available() -> bool:
    return pa is not None and pq is not None


def _bucket_dir(cache_root: str, symbol: str, interval: str, year: int, month: int) -> str:
    return os.path.join(cache_root, symbol.upper(), interval, f"{year:04d}{month:02d}")


def _bucket_path(cache_root: str, symbol: str, interval: str, year: int, month: int) -> str:
    return os.path.join(_bucket_dir(cache_root, symbol, interval, year, month), "klines.parquet")


def _month_buckets(start_ts: int, end_ts: int) -> Iterator[Tuple[int, int, int, int]]:
    """Yield (year, month, bucket_start_ms, bucket_end_ms) covering the range."""
    cur = datetime.fromtimestamp(start_ts / 1000.0, tz=timezone.utc).replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    )
    end_dt = datetime.fromtimestamp(end_ts / 1000.0, tz=timezone.utc)
    while int(cur.timestamp() * 1000) <= end_ts:
        if cur.month == 12:
            nxt = cur.replace(year=cur.year + 1, month=1)
        else:
            nxt = cur.replace(month=cur.month + 1)
        bucket_start = int(cur.timestamp() * 1000)
        bucket_end = int(nxt.timestamp() * 1000) - 1
        yield cur.year, cur.month, max(bucket_start, start_ts), min(bucket_end, end_ts)
        cur = nxt
        if cur > end_dt:
            break


_COLUMNS = [
    "symbol",
    "interval",
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "num_trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore_field",
]


def materialize_window(
    db_path: str,
    symbol: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    cache_root: str = CACHE_ROOT_DEFAULT,
    overwrite: bool = False,
) -> List[str]:
    """Materialize a monthly Parquet cache for the given window.

    Returns the list of Parquet files written or already present. If pyarrow
    is unavailable, returns an empty list (the caller should fall back to
    SQLite reads).
    """
    if not is_available():
        return []
    paths: List[str] = []
    for year, month, b_start, b_end in _month_buckets(start_ts, end_ts):
        out_path = _bucket_path(cache_root, symbol, interval, year, month)
        if not overwrite and os.path.exists(out_path):
            paths.append(out_path)
            continue
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        rows = list(
            iter_query_klines(
                db_path,
                symbol=symbol,
                interval=interval,
                start_ts=b_start,
                end_ts=b_end,
            )
        )
        if not rows:
            continue
        columns: Dict[str, List[Any]] = {name: [] for name in _COLUMNS}
        for r in rows:
            for i, name in enumerate(_COLUMNS):
                columns[name].append(r[i])
        table = pa.table(columns)
        pq.write_table(table, out_path, compression="zstd")
        paths.append(out_path)
    return paths


def read_window(
    symbol: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    cache_root: str = CACHE_ROOT_DEFAULT,
) -> Optional[List[Tuple]]:
    """Read a window from the Parquet cache. Returns None if not available."""
    if not is_available():
        return None
    rows: List[Tuple] = []
    for year, month, _b_start, _b_end in _month_buckets(start_ts, end_ts):
        path = _bucket_path(cache_root, symbol, interval, year, month)
        if not os.path.exists(path):
            return None  # cache hole; caller should rebuild or fall back
        table = pq.read_table(path)
        data = table.to_pydict()
        n = len(data["open_time"])
        for i in range(n):
            ts = int(data["open_time"][i])
            if ts < start_ts or ts > end_ts:
                continue
            rows.append(tuple(data[col][i] for col in _COLUMNS))
    return rows
