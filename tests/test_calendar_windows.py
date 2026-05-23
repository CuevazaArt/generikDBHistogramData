"""Focused tests for :mod:`backtest.calendar_windows`.

These tests cover the contract that the strict-run ``--chain-by-month``
mode depends on: deterministic, year-agnostic monthly windows clipped to a
``[start_ts, end_ts]`` range. They were added when the legacy
``_MONTH_WINDOWS_2024_1S`` constant was replaced by a dynamic generator.
"""
from __future__ import annotations

from datetime import datetime, timezone
from itertools import pairwise

import pytest

from backtest.calendar_windows import (
    format_window_name,
    month_end_ms,
    month_start_ms,
    monthly_windows,
)


def _utc_ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc).timestamp() * 1000)


# ---------------------------------------------------------------------------
# Boundary helpers
# ---------------------------------------------------------------------------
def test_month_start_and_end_match_legacy_2024_1s_bounds():
    """The dynamic helpers must reproduce the bounds previously hardcoded in
    ``_MONTH_WINDOWS_2024_1S`` (XRPUSDT 1s 2024 monthly manifests)."""
    legacy_2024 = [
        (1, 1704067200000, 1706745599000),
        (2, 1706745600000, 1709251199000),
        (3, 1709251200000, 1711929599000),
        (4, 1711929600000, 1714521599000),
        (5, 1714521600000, 1717199999000),
        (6, 1717200000000, 1719791999000),
        (7, 1719792000000, 1722470399000),
        (8, 1722470400000, 1725148799000),
        (9, 1725148800000, 1727740799000),
        (10, 1727740800000, 1730419199000),
        (11, 1730419200000, 1733011199000),
        (12, 1733011200000, 1735689599000),
    ]
    for month, expected_start, expected_end in legacy_2024:
        assert month_start_ms(2024, month) == expected_start
        assert month_end_ms(2024, month) == expected_end


def test_format_window_name_supports_yyyy_mm_and_myyymm():
    assert format_window_name(2024, 1) == "2024-01"
    assert format_window_name(2025, 12) == "2025-12"
    assert format_window_name(2024, 1, name_format="MYYYYMM") == "M202401"
    assert format_window_name(2025, 12, name_format="MYYYYMM") == "M202512"
    with pytest.raises(ValueError):
        format_window_name(2024, 1, name_format="bogus")


# ---------------------------------------------------------------------------
# 1) Full year 2024 -> 12 windows (compat with old behavior)
# ---------------------------------------------------------------------------
def test_monthly_windows_full_2024_produces_12_calendar_months():
    start_ts = _utc_ms(2024, 1, 1)
    end_ts = _utc_ms(2024, 12, 31, 23, 59, 59)
    windows = monthly_windows(start_ts, end_ts)
    assert len(windows) == 12
    expected_names = [f"2024-{m:02d}" for m in range(1, 13)]
    assert [w[0] for w in windows] == expected_names
    # First and last bounds match the legacy 2024 1s constant.
    assert windows[0] == ("2024-01", 1704067200000, 1706745599000)
    assert windows[-1] == ("2024-12", 1733011200000, 1735689599000)
    # Windows are contiguous: each end_ms + 1000 == next start_ms.
    for prev, nxt in pairwise(windows):
        assert prev[2] + 1000 == nxt[1]


# ---------------------------------------------------------------------------
# 2) Full year 2025 -> 12 windows (year-agnostic regression)
# ---------------------------------------------------------------------------
def test_monthly_windows_full_2025_produces_12_calendar_months():
    start_ts = _utc_ms(2025, 1, 1)
    end_ts = _utc_ms(2025, 12, 31, 23, 59, 59)
    windows = monthly_windows(start_ts, end_ts)
    assert len(windows) == 12
    assert [w[0] for w in windows] == [f"2025-{m:02d}" for m in range(1, 13)]
    # 2025 is a non-leap year: February has 28 days.
    feb_2025 = next(w for w in windows if w[0] == "2025-02")
    assert feb_2025 == (
        "2025-02",
        _utc_ms(2025, 2, 1),
        _utc_ms(2025, 2, 28, 23, 59, 59),
    )
    # Last window ends at the 2025-12-31 23:59:59 UTC second.
    assert windows[-1][2] == 1767225599000


# ---------------------------------------------------------------------------
# 3) Multi-year short range (2024-11 -> 2025-02) -> 4 windows
# ---------------------------------------------------------------------------
def test_monthly_windows_spans_year_boundary_2024q4_2025q1():
    start_ts = _utc_ms(2024, 11, 1)
    end_ts = _utc_ms(2025, 2, 28, 23, 59, 59)
    windows = monthly_windows(start_ts, end_ts)
    assert [w[0] for w in windows] == ["2024-11", "2024-12", "2025-01", "2025-02"]
    assert windows[0] == ("2024-11", 1730419200000, 1733011199000)
    assert windows[1] == ("2024-12", 1733011200000, 1735689599000)
    assert windows[2] == ("2025-01", 1735689600000, _utc_ms(2025, 1, 31, 23, 59, 59))
    assert windows[3] == ("2025-02", _utc_ms(2025, 2, 1), _utc_ms(2025, 2, 28, 23, 59, 59))


# ---------------------------------------------------------------------------
# 4) Intra-month clamp on first/last window
# ---------------------------------------------------------------------------
def test_monthly_windows_clamps_partial_first_and_last_month():
    start_ts = _utc_ms(2024, 1, 15)
    end_ts = _utc_ms(2024, 3, 15, 23, 59, 59)
    windows = monthly_windows(start_ts, end_ts)
    assert [w[0] for w in windows] == ["2024-01", "2024-02", "2024-03"]
    assert windows[0] == ("2024-01", start_ts, 1706745599000)
    assert windows[1] == ("2024-02", 1706745600000, 1709251199000)
    assert windows[-1] == ("2024-03", 1709251200000, end_ts)


# ---------------------------------------------------------------------------
# Filter and validation behavior
# ---------------------------------------------------------------------------
def test_monthly_windows_respects_from_through_within_single_year():
    start_ts = _utc_ms(2024, 1, 1)
    end_ts = _utc_ms(2024, 12, 31, 23, 59, 59)
    windows = monthly_windows(start_ts, end_ts, from_month=3, through_month=6)
    assert [w[0] for w in windows] == ["2024-03", "2024-04", "2024-05", "2024-06"]


def test_monthly_windows_applies_month_filter_per_year_in_multi_year_range():
    start_ts = _utc_ms(2024, 1, 1)
    end_ts = _utc_ms(2025, 12, 31, 23, 59, 59)
    windows = monthly_windows(start_ts, end_ts, from_month=11, through_month=12)
    assert [w[0] for w in windows] == ["2024-11", "2024-12", "2025-11", "2025-12"]


def test_monthly_windows_rejects_inverted_range():
    start_ts = _utc_ms(2024, 6, 1)
    end_ts = _utc_ms(2024, 5, 1)
    with pytest.raises(ValueError):
        monthly_windows(start_ts, end_ts)


def test_monthly_windows_rejects_invalid_month_filter():
    start_ts = _utc_ms(2024, 1, 1)
    end_ts = _utc_ms(2024, 12, 31, 23, 59, 59)
    with pytest.raises(ValueError):
        monthly_windows(start_ts, end_ts, from_month=0, through_month=12)
    with pytest.raises(ValueError):
        monthly_windows(start_ts, end_ts, from_month=8, through_month=3)


def test_monthly_windows_raises_when_filter_excludes_everything():
    start_ts = _utc_ms(2024, 6, 1)
    end_ts = _utc_ms(2024, 6, 30, 23, 59, 59)
    with pytest.raises(ValueError):
        monthly_windows(start_ts, end_ts, from_month=1, through_month=2)


def test_monthly_windows_supports_myyymm_name_format():
    start_ts = _utc_ms(2024, 11, 1)
    end_ts = _utc_ms(2025, 2, 28, 23, 59, 59)
    windows = monthly_windows(start_ts, end_ts, name_format="MYYYYMM")
    assert [w[0] for w in windows] == ["M202411", "M202412", "M202501", "M202502"]
