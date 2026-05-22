"""Smoke tests for the incremental architecture changes.

Covers:
- `recommend_n_jobs` supports the new `adaptive_80` mode.
- `ResourceGuardConfig` defaults to 80% caps.
- `ResourceGuard.should_scale_up()` exists and behaves with no telemetry.
- `data_integrity.find_gaps` returns the expected gap pairs.
- `insert_bt_events` accepts `batch_size` and persists correctly.
- `BinanceDownloader._retry_after_seconds` returns a bounded delay.
- `iter_query_klines` yields by batches without loading all in memory.
- `_add_custom_smas` works correctly without cloning candles.
- `backtest.scheduler` API can be imported and instantiated.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

from backtest.data_integrity import find_gaps, interval_step_ms, window_stats
from backtest.engine import _add_custom_smas
from backtest.guards import ResourceGuard, ResourceGuardConfig
from backtest.resources import recommend_n_jobs
from backtest.scheduler import BranchSpec, SchedulerConfig
from binance_hist_downloader import _retry_after_seconds
from db import init_db, insert_bt_events, create_bt_run, iter_query_klines


def test_recommend_adaptive_80_returns_positive_int():
    n = recommend_n_jobs(mode="adaptive_80", dataset_candles=1_000_000)
    assert isinstance(n, int)
    assert n >= 1


def test_guard_defaults_80_and_scale_up_optimistic_without_telemetry():
    cfg = ResourceGuardConfig()
    assert cfg.cpu_cap_pct == 80.0
    assert cfg.ram_cap_pct == 80.0
    guard = ResourceGuard(cfg)
    # When psutil is unavailable, `_enabled` becomes False; should_scale_up
    # returns True (optimistic ramp-up) and should_throttle returns False.
    if not guard._enabled:  # pragma: no cover - depends on environment
        assert guard.should_scale_up() is True
        assert guard.should_throttle() is False


def test_find_gaps_detects_missing_minute():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "klines.db")
        init_db(db)
        conn = sqlite3.connect(db)
        try:
            step = interval_step_ms("1m")
            base = 1_700_000_000_000
            rows = [
                ("BTCUSDT", "1m", base + i * step, 1.0, 1.0, 1.0, 1.0, 1.0, base + i * step + step - 1, None, 1, None, None, "")
                for i in range(10)
                if i not in (4, 5)  # drop two consecutive minutes
            ]
            conn.executemany(
                """
                INSERT INTO klines (symbol, interval, open_time, open, high, low, close, volume,
                    close_time, quote_asset_volume, num_trades, taker_buy_base, taker_buy_quote, ignore_field)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()

        stats = window_stats(db, "BTCUSDT", "1m")
        assert stats.count == 8
        gaps = find_gaps(db, "BTCUSDT", "1m")
        assert len(gaps) == 1
        gap_start, gap_end = gaps[0]
        assert gap_start == base + 4 * step
        assert gap_end == base + 5 * step


def test_insert_bt_events_batched_persists_all():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "klines.db")
        init_db(db)
        run_id = create_bt_run(
            db,
            strategy_name="sma_cross",
            symbol="BTCUSDT",
            interval="1m",
            start_ts=0,
            end_ts=10,
            initial_cash=1000.0,
            fee_rate=0.0,
            slippage_bps=0.0,
            config={},
        )
        events = [
            {
                "trial_id": None,
                "seq": i,
                "event_time": i,
                "event_type": "hold",
                "side": None,
                "price": None,
                "qty": None,
                "cash": 1000.0,
                "equity": 1000.0,
                "position_qty": 0.0,
                "payload": {},
            }
            for i in range(20)
        ]
        insert_bt_events(db, run_id=run_id, events=events, batch_size=7)
        conn = sqlite3.connect(db)
        try:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM bt_events WHERE run_id=?", (run_id,)
            ).fetchone()
        finally:
            conn.close()
        assert count == len(events)


def test_retry_after_seconds_is_bounded():
    for attempt in range(0, 6):
        delay = _retry_after_seconds(None, base=0.5, attempt=attempt)
        assert delay >= 0
        assert delay <= 65  # cap (60) + jitter slack


def test_iter_query_klines_yields_in_batches():
    with tempfile.TemporaryDirectory() as tmp:
        db = os.path.join(tmp, "klines.db")
        init_db(db)
        conn = sqlite3.connect(db)
        try:
            step = interval_step_ms("1m")
            base = 1_700_000_000_000
            rows = [
                ("BTCUSDT", "1m", base + i * step, 1.0, 1.0, 1.0, 1.0, 1.0, base + i * step + step - 1, None, 1, None, None, "")
                for i in range(25)
            ]
            conn.executemany(
                """
                INSERT INTO klines (symbol, interval, open_time, open, high, low, close, volume,
                    close_time, quote_asset_volume, num_trades, taker_buy_base, taker_buy_quote, ignore_field)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
        finally:
            conn.close()
        collected = list(iter_query_klines(db, "BTCUSDT", "1m", fetch_size=4))
        assert len(collected) == 25


def test_add_custom_smas_no_clone_writes_keys():
    candles = [{"price_source": float(i), "close": float(i)} for i in range(5)]
    _add_custom_smas(candles, fast=2, slow=3)
    assert "sma_2" in candles[-1]
    assert "sma_3" in candles[-1]
    assert candles[-1]["sma_2"] is not None
    assert candles[-1]["sma_3"] is not None


def test_scheduler_dataclasses_construct():
    spec = BranchSpec(name="x", command=["python", "-V"], log_path="x.log")
    cfg = SchedulerConfig(
        repo_root=".",
        master_log_jsonl="MASTER.jsonl",
    )
    assert spec.name == "x"
    assert cfg.cpu_cap_pct == 80.0
    assert cfg.ram_cap_pct == 80.0
