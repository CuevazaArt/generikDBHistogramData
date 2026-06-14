"""Simple walk-forward helper for honesty checks on optimized setups.

Concept (single split, easy for casual users):
- Take the historical window the user selects.
- Use the first `train_pct` portion to run Optuna (in-sample).
- Use the remaining portion to backtest the best params (out-of-sample).
- Report the metric on both sides so the user can compare.

The goal is to spot strategies that overfit: they look great in-sample but
underperform out-of-sample.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional, Type

from backtest.data_feed import candles_to_dicts, load_candles
from backtest.engine import EngineConfig
from backtest.optimize import OptimizationConfig, optimize_strategy
from backtest.runner import execute_and_persist
from backtest.strategy_base import StrategyBase


@dataclass
class WalkForwardResult:
    train_run_id: int | None
    test_run_id: int | None
    train_metric: float
    test_metric: float
    metric_name: str
    best_params: Dict[str, Any]
    split_ts: int | None


def _candle_window_bounds(
    db_path: str,
    symbol: str,
    interval: str,
    start_ts: int | None,
    end_ts: int | None,
    train_pct: float,
) -> tuple[int, int, int]:
    candles = candles_to_dicts(
        load_candles(db_path, symbol=symbol, interval=interval, start_ts=start_ts, end_ts=end_ts)
    )
    if len(candles) < 10:
        raise ValueError(
            f"No hay velas suficientes para walk-forward: {len(candles)} (minimo 10)."
        )
    pct = max(0.1, min(0.9, float(train_pct)))
    split_idx = max(1, int(len(candles) * pct))
    train_start = int(candles[0]["open_time"])
    split_ts = int(candles[split_idx]["open_time"])
    train_end = split_ts - 1
    test_start = split_ts
    test_end = int(candles[-1]["open_time"]) + 1
    return train_start, train_end, test_start, test_end, split_ts


def run_walkforward(
    db_path: str,
    study_name: str,
    strategy_cls: Type[StrategyBase],
    base_config: EngineConfig,
    trials: int,
    n_jobs: int,
    train_pct: float = 0.7,
    timeout: int | None = None,
    search_overrides: Dict[str, Any] | None = None,
    optimization: OptimizationConfig | None = None,
) -> WalkForwardResult:
    opt = optimization or OptimizationConfig()
    (
        train_start,
        train_end,
        test_start,
        test_end,
        split_ts,
    ) = _candle_window_bounds(
        db_path=db_path,
        symbol=base_config.symbol,
        interval=base_config.interval,
        start_ts=base_config.start_ts,
        end_ts=base_config.end_ts,
        train_pct=train_pct,
    )

    train_cfg = EngineConfig(**base_config.__dict__)
    train_cfg.start_ts = train_start
    train_cfg.end_ts = train_end

    study = optimize_strategy(
        db_path=db_path,
        study_name=study_name,
        strategy_cls=strategy_cls,
        base_config=train_cfg,
        trials=trials,
        n_jobs=n_jobs,
        timeout=timeout,
        search_overrides=search_overrides,
        optimization=opt,
    )
    best_params = dict(study.best_params)

    # Train run with best params (persisted as a regular run for the dashboard).
    train_result = execute_and_persist(
        config=train_cfg,
        strategy_cls=strategy_cls,
        strategy_params=best_params,
    )

    test_cfg = EngineConfig(**base_config.__dict__)
    test_cfg.start_ts = test_start
    test_cfg.end_ts = test_end
    test_result = execute_and_persist(
        config=test_cfg,
        strategy_cls=strategy_cls,
        strategy_params=best_params,
    )

    metric_name = opt.objective_metric
    train_metric = float(train_result.metrics.get(metric_name, 0.0))
    test_metric = float(test_result.metrics.get(metric_name, 0.0))
    return WalkForwardResult(
        train_run_id=train_result.run_id,
        test_run_id=test_result.run_id,
        train_metric=train_metric,
        test_metric=test_metric,
        metric_name=metric_name,
        best_params=best_params,
        split_ts=split_ts,
    )
