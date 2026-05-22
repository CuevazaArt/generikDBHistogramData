"""Data integrity helpers for the local klines store.

Lets the downloader (and CLI tools) ask questions like:
- What is the last open_time we already have for this symbol/interval?
- Does this window have gaps? Where?
- Do the gaps match expected step size for the interval?

These helpers are read-only and safe to run while a downloader writes.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import List, Optional, Tuple


_INTERVAL_TO_MS = {
    "1s": 1_000,
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}


def interval_step_ms(interval: str) -> int:
    return int(_INTERVAL_TO_MS.get(interval.strip().lower(), 60_000))


@dataclass
class WindowStats:
    symbol: str
    interval: str
    count: int
    min_open_time: Optional[int]
    max_open_time: Optional[int]
    expected_step_ms: int


def window_stats(db_path: str, symbol: str, interval: str, start_ts: Optional[int] = None, end_ts: Optional[int] = None) -> WindowStats:
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        sql = "SELECT COUNT(*), MIN(open_time), MAX(open_time) FROM klines WHERE symbol=? AND interval=?"
        params: List = [symbol, interval]
        if start_ts is not None:
            sql += " AND open_time>=?"
            params.append(int(start_ts))
        if end_ts is not None:
            sql += " AND open_time<=?"
            params.append(int(end_ts))
        row = cur.execute(sql, params).fetchone()
    finally:
        conn.close()
    return WindowStats(
        symbol=symbol,
        interval=interval,
        count=int(row[0] or 0),
        min_open_time=int(row[1]) if row[1] is not None else None,
        max_open_time=int(row[2]) if row[2] is not None else None,
        expected_step_ms=interval_step_ms(interval),
    )


def next_continuous_open_time(
    db_path: str,
    symbol: str,
    interval: str,
) -> Optional[int]:
    """Return the timestamp immediately after the last known kline.

    Useful to resume downloads from where the local DB left off.
    """
    stats = window_stats(db_path, symbol, interval)
    if stats.max_open_time is None:
        return None
    return int(stats.max_open_time) + stats.expected_step_ms


def find_gaps(
    db_path: str,
    symbol: str,
    interval: str,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    max_gaps: int = 1000,
) -> List[Tuple[int, int]]:
    """Detect gaps within [start_ts, end_ts] returning (gap_start, gap_end) pairs.

    A gap is defined as a missing open_time block based on the expected step
    for the given interval. Limits output to `max_gaps` to avoid pathological
    cases. Returns an empty list if no data is found.
    """
    step = interval_step_ms(interval)
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        cur = conn.cursor()
        sql = "SELECT open_time FROM klines WHERE symbol=? AND interval=?"
        params: List = [symbol, interval]
        if start_ts is not None:
            sql += " AND open_time>=?"
            params.append(int(start_ts))
        if end_ts is not None:
            sql += " AND open_time<=?"
            params.append(int(end_ts))
        sql += " ORDER BY open_time ASC"
        cur.execute(sql, params)
        prev: Optional[int] = None
        gaps: List[Tuple[int, int]] = []
        for (ts,) in cur:
            ts = int(ts)
            if prev is not None and ts - prev > step:
                gap_start = prev + step
                gap_end = ts - step
                gaps.append((gap_start, gap_end))
                if len(gaps) >= max_gaps:
                    break
            prev = ts
    finally:
        conn.close()
    return gaps


def coverage_ratio(
    db_path: str,
    symbol: str,
    interval: str,
    start_ts: int,
    end_ts: int,
) -> float:
    """Return ratio of present candles vs expected in window (0..1)."""
    stats = window_stats(db_path, symbol, interval, start_ts=start_ts, end_ts=end_ts)
    span = max(1, int(end_ts) - int(start_ts))
    expected = max(1, span // interval_step_ms(interval))
    return min(1.0, stats.count / expected)
