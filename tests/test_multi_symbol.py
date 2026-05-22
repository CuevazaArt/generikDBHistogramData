"""Fase 4 multi-symbol runner tests."""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from backtest.aggregator import aggregate_multi_symbol_metrics
from backtest.engine import EngineConfig
from backtest.multi_symbol import MultiSymbolConfig, run_multi_symbol


_BASE_TS = 1_704_067_200_000  # 2024-01-01T00:00:00Z


def _seed_synthetic_klines(
    db_path: str, symbol: str, *, rows: int = 480, seed_offset: int = 0
) -> None:
    import db as legacy_db

    legacy_db.init_db(db_path)
    insert_rows = []
    for i in range(rows):
        open_t = _BASE_TS + i * 3_600_000
        base = 50_000.0 + 200.0 * math.sin((i + seed_offset) / 17.0)
        op = base
        cl = base + 25.0 * math.cos((i + seed_offset) / 11.0)
        hi = max(op, cl) + 15.0
        lo = min(op, cl) - 15.0
        vol = 10.0
        ct = open_t + 3_600_000 - 1
        qv = vol * (op + cl) / 2.0
        nt = 100
        tb = vol * 0.55
        tq = qv * 0.55
        insert_rows.append((open_t, op, hi, lo, cl, vol, ct, qv, nt, tb, tq, "0"))
    legacy_db.insert_klines(db_path, symbol, "1h", insert_rows)


def test_run_multi_symbol_serial(tmp_path: Path) -> None:
    db_path = str(tmp_path / "multi.db")
    _seed_synthetic_klines(db_path, "BTCUSDT", rows=480, seed_offset=0)
    _seed_synthetic_klines(db_path, "XRPUSDT", rows=480, seed_offset=7)

    cfg = MultiSymbolConfig(
        symbols=["BTCUSDT", "XRPUSDT"],
        interval="1h",
        start_ts=None,
        end_ts=None,
        initial_cash_per_symbol=10_000.0,
        share_cash_pool=False,
    )
    engine_cfg = EngineConfig(
        db_path=db_path,
        symbol="placeholder",
        interval="1h",
        initial_cash=10_000.0,
        fee_rate=0.001,
        slippage_bps=2.0,
        events_mode="minimal",
        sma_fast=10,
        sma_slow=30,
    )

    result = run_multi_symbol(
        cfg=cfg,
        strategy_name="sma_cross",
        strategy_params={"fast": 10, "slow": 30},
        engine_config=engine_cfg,
        db_path=db_path,
    )

    assert len(result.per_symbol) == 2
    for symbol in ("BTCUSDT", "XRPUSDT"):
        payload = result.per_symbol[symbol]
        assert isinstance(payload["run_id"], int) and payload["run_id"] > 0
        metrics = payload["metrics"]
        for required in ("total_return", "sharpe", "final_equity", "initial_cash"):
            assert required in metrics

    aggregated = result.aggregated
    expected_keys = {
        "n_symbols",
        "mean_total_return",
        "median_total_return",
        "worst_symbol",
        "worst_symbol_total_return",
        "best_symbol",
        "best_symbol_total_return",
        "joint_capital_curve",
        "dispersion_pct",
        "per_symbol_summary",
    }
    assert expected_keys.issubset(aggregated.keys())
    assert aggregated["n_symbols"] == 2
    assert aggregated["joint_capital_curve"] is None
    assert aggregated["best_symbol"] in {"BTCUSDT", "XRPUSDT"}
    assert aggregated["worst_symbol"] in {"BTCUSDT", "XRPUSDT"}


def test_aggregate_multi_symbol() -> None:
    per_symbol = {
        "BTCUSDT": {"run_id": 1, "metrics": {"total_return": 0.15, "sharpe": 1.0, "win_rate": 0.6, "num_trades": 25}},
        "XRPUSDT": {"run_id": 2, "metrics": {"total_return": -0.05, "sharpe": -0.2, "win_rate": 0.4, "num_trades": 30}},
        "ETHUSDT": {"run_id": 3, "metrics": {"total_return": 0.30, "sharpe": 1.4, "win_rate": 0.55, "num_trades": 40}},
    }
    agg = aggregate_multi_symbol_metrics(per_symbol)
    assert agg["n_symbols"] == 3
    assert agg["best_symbol"] == "ETHUSDT"
    assert agg["worst_symbol"] == "XRPUSDT"
    assert agg["best_symbol_total_return"] == pytest.approx(0.30)
    assert agg["worst_symbol_total_return"] == pytest.approx(-0.05)
    assert agg["dispersion_pct"] == pytest.approx(0.30 - (-0.05))
    assert agg["mean_total_return"] == pytest.approx((0.15 - 0.05 + 0.30) / 3)
    summary_symbols = {row["symbol"] for row in agg["per_symbol_summary"]}
    assert summary_symbols == {"BTCUSDT", "XRPUSDT", "ETHUSDT"}


def test_share_cash_pool_raises(tmp_path: Path) -> None:
    cfg = MultiSymbolConfig(
        symbols=["BTCUSDT"],
        interval="1h",
        start_ts=None,
        end_ts=None,
        initial_cash_per_symbol=10_000.0,
        share_cash_pool=True,
    )
    engine_cfg = EngineConfig(
        db_path=str(tmp_path / "ignored.db"),
        symbol="BTCUSDT",
        interval="1h",
    )
    with pytest.raises(NotImplementedError):
        run_multi_symbol(
            cfg=cfg,
            strategy_name="sma_cross",
            strategy_params={"fast": 10, "slow": 30},
            engine_config=engine_cfg,
            db_path=str(tmp_path / "ignored.db"),
        )
