"""High-level run orchestration (engine + storage)."""
from typing import Dict, Optional, Type

from backtest.engine import BacktestResult, EngineConfig, run_backtest
from backtest.storage import create_run, finish_run, persist_run_events, persist_run_metrics
from backtest.strategy_base import StrategyBase
from db import init_db


def execute_and_persist(
    config: EngineConfig,
    strategy_cls: Type[StrategyBase],
    strategy_params: Optional[Dict] = None,
    trial_id: Optional[int] = None,
) -> BacktestResult:
    init_db(config.db_path)
    strategy_params = strategy_params or {}
    run_id = create_run(
        db_path=config.db_path,
        strategy_name=strategy_cls.name,
        symbol=config.symbol,
        interval=config.interval,
        start_ts=config.start_ts,
        end_ts=config.end_ts,
        initial_cash=config.initial_cash,
        fee_rate=config.fee_rate,
        slippage_bps=config.slippage_bps,
        config={
            "engine": config.__dict__,
            "strategy": strategy_params,
        },
    )
    try:
        result = run_backtest(
            config=config,
            strategy_cls=strategy_cls,
            strategy_params=strategy_params,
            run_id=run_id,
            trial_id=trial_id,
        )
        persist_run_events(config.db_path, run_id=run_id, events=result.events)
        persist_run_metrics(
            config.db_path,
            run_id=run_id,
            metrics=result.metrics,
            trial_id=trial_id,
            extra={"symbol": config.symbol, "interval": config.interval},
        )
        finish_run(config.db_path, run_id=run_id, status="completed")
        result.run_id = run_id
        return result
    except Exception:
        finish_run(config.db_path, run_id=run_id, status="failed")
        raise

