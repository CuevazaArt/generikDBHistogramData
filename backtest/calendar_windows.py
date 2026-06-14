"""Generic calendar window helpers (year-agnostic).

This module provides reusable helpers for splitting a UTC millisecond range
``[start_ts, end_ts]`` into calendar-aligned subwindows. The primary use case
is the strict-run ``--chain-by-month`` mode in
``scripts/run_xrpusdt_2024_dorothy_strict.py``, which previously hardcoded the
2024 month bounds. Keeping the logic here lets it be used by any future
script (any year, any symbol/interval) and unit-tested in isolation.

Conventions
-----------

- All timestamps are integer **UTC milliseconds** (Binance kline convention).
- A "calendar month" window starts at ``YYYY-MM-01 00:00:00.000 UTC`` and
  ends at ``YYYY-(MM+1)-01 00:00:00.000 UTC - 1000 ms``, i.e. the last full
  second of the month. This matches the boundaries already used by the
  Parquet kline manifests (see ``data/klines/...``).
- Windows are clipped to the requested ``[start_ts, end_ts]`` range, so a
  partial first/last month is allowed and reproducible.
- Window names use the unambiguous format ``YYYY-MM`` by default
  (e.g. ``"2024-01"``), or ``MYYYYMM`` when ``name_format="MYYYYMM"``
  (e.g. ``"M202401"``). The legacy ``M01``/``M12`` short labels are
  intentionally removed because they were ambiguous across years.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Tuple

__all__ = [
    "format_window_name",
    "month_end_ms",
    "month_start_ms",
    "monthly_windows",
]


_VALID_NAME_FORMATS = ("YYYY-MM", "MYYYYMM")


def month_start_ms(year: int, month: int) -> int:
    """Return the UTC millisecond timestamp for ``YYYY-MM-01 00:00:00.000 UTC``."""
    if not (1 <= int(month) <= 12):
        raise ValueError(f"month must be in 1..12, got {month}")
    dt = datetime(int(year), int(month), 1, tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def month_end_ms(year: int, month: int) -> int:
    """Return the UTC millisecond timestamp of the last full second of the month.

    Equivalent to ``month_start_ms(next_month) - 1000``. This matches the
    closed-interval convention used by the existing manifests (the last
    1-second bar of the month opens at this timestamp).
    """
    if not (1 <= int(month) <= 12):
        raise ValueError(f"month must be in 1..12, got {month}")
    if int(month) == 12:
        next_year, next_month = int(year) + 1, 1
    else:
        next_year, next_month = int(year), int(month) + 1
    return month_start_ms(next_year, next_month) - 1000


def format_window_name(year: int, month: int, name_format: str = "YYYY-MM") -> str:
    """Return the canonical name for a monthly window.

    Two formats are supported:
      - ``"YYYY-MM"`` (default): ``"2024-01"``, ``"2025-12"``.
      - ``"MYYYYMM"``: ``"M202401"``, ``"M202512"``.
    Both encode the year explicitly so windows from different years never
    collide (unlike the legacy ``M01..M12`` labels).
    """
    if name_format not in _VALID_NAME_FORMATS:
        raise ValueError(
            f"name_format must be one of {_VALID_NAME_FORMATS}, got {name_format!r}"
        )
    if name_format == "MYYYYMM":
        return f"M{int(year):04d}{int(month):02d}"
    return f"{int(year):04d}-{int(month):02d}"


def monthly_windows(
    start_ts: int,
    end_ts: int,
    *,
    from_month: int = 1,
    through_month: int = 12,
    name_format: str = "YYYY-MM",
) -> List[Tuple[str, int, int]]:
    """Generate calendar-month subwindows covering ``[start_ts, end_ts]``.

    Parameters
    ----------
    start_ts, end_ts:
        UTC millisecond bounds of the global range. ``start_ts < end_ts``.
    from_month, through_month:
        Optional month-of-year filter (``1..12`` inclusive). Useful when the
        global range is within a single calendar year. When the range spans
        multiple years, the filter is applied **per year**: only months whose
        ``month`` falls inside ``[from_month, through_month]`` are emitted,
        for **every** year touched by the range. With the defaults
        ``(1, 12)`` no months are filtered out, so multi-year ranges are
        fully covered.
    name_format:
        ``"YYYY-MM"`` (default) or ``"MYYYYMM"``. See :func:`format_window_name`.

    Returns
    -------
    list of ``(name, start_ms, end_ms)`` tuples in chronological order. Each
    window is clipped to ``[start_ts, end_ts]``, so the first and last entries
    may be partial months when the global range starts/ends mid-month.

    Raises
    ------
    ValueError:
        If ``start_ts >= end_ts``, if month bounds are out of range, or if no
        month overlaps the requested range (after applying the filter).
    """
    if int(start_ts) >= int(end_ts):
        raise ValueError(f"start_ts must be < end_ts (got {start_ts} >= {end_ts})")
    fm = int(from_month)
    tm = int(through_month)
    if not (1 <= fm <= 12 and 1 <= tm <= 12):
        raise ValueError(
            f"from_month/through_month must be in 1..12, got {from_month}/{through_month}"
        )
    if fm > tm:
        raise ValueError(
            f"from_month ({fm}) must be <= through_month ({tm})"
        )

    start_dt = datetime.fromtimestamp(int(start_ts) / 1000.0, tz=timezone.utc)
    end_dt = datetime.fromtimestamp(int(end_ts) / 1000.0, tz=timezone.utc)
    cur_year, cur_month = start_dt.year, start_dt.month
    end_year, end_month = end_dt.year, end_dt.month

    windows: List[Tuple[str, int, int]] = []
    while (cur_year, cur_month) <= (end_year, end_month):
        if fm <= cur_month <= tm:
            m_start = month_start_ms(cur_year, cur_month)
            m_end = month_end_ms(cur_year, cur_month)
            clipped_start = max(m_start, int(start_ts))
            clipped_end = min(m_end, int(end_ts))
            if clipped_start <= clipped_end:
                windows.append(
                    (
                        format_window_name(cur_year, cur_month, name_format),
                        clipped_start,
                        clipped_end,
                    )
                )
        if cur_month == 12:
            cur_year += 1
            cur_month = 1
        else:
            cur_month += 1

    if not windows:
        raise ValueError(
            "No monthly windows overlap the requested range "
            f"(start_ts={start_ts}, end_ts={end_ts}, "
            f"from_month={fm}, through_month={tm})"
        )
    return windows
