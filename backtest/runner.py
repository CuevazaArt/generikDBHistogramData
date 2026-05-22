"""High-level run orchestration (engine + storage)."""
import os
from typing import Dict, Optional, Type

from backtest.engine import BacktestResult, EngineConfig, run_backtest
from backtest.report_paths import run_report_dir
from backtest.storage import create_run, finish_run, persist_run_events, persist_run_metrics
from backtest.strategy_base import StrategyBase
from db import init_db


def _parquet_events_enabled() -> bool:
    return os.getenv("BACKTEST_EVENTS_PARQUET", "0").strip().lower() in {"1", "true", "yes", "on"}


def execute_and_persist(
    config: EngineConfig,
    strategy_cls: Type[StrategyBase],
    strategy_params: Optional[Dict] = None,
    trial_id: Optional[int] = None,
    initial_state: Optional[Dict] = None,
    events_batch_size: int = 5000,
) -> BacktestResult:
    init_db(config.db_path)
    strategy_params = strategy_params or {}
    run_cfg = EngineConfig(**config.__dict__)
    run_cfg.initial_state = initial_state or config.initial_state
    run_id = create_run(
        db_path=run_cfg.db_path,
        strategy_name=strategy_cls.name,
        symbol=run_cfg.symbol,
        interval=run_cfg.interval,
        start_ts=run_cfg.start_ts,
        end_ts=run_cfg.end_ts,
        initial_cash=run_cfg.initial_cash,
        fee_rate=run_cfg.fee_rate,
        slippage_bps=run_cfg.slippage_bps,
        config={
            "engine": run_cfg.__dict__,
            "strategy": strategy_params,
        },
    )
    try:
        result = run_backtest(
            config=run_cfg,
            strategy_cls=strategy_cls,
            strategy_params=strategy_params,
            run_id=run_id,
            trial_id=trial_id,
        )
        persist_run_events(
            run_cfg.db_path,
            run_id=run_id,
            events=result.events,
            batch_size=events_batch_size,
        )
        if _parquet_events_enabled() and (run_cfg.events_mode or "full").strip().lower() == "full":
            try:
                from backtest.events_parquet import dump_events_to_parquet  # local import

                target_dir = run_report_dir("reports", run_id)
                dump_events_to_parquet(
                    output_path=os.path.join(target_dir, "events.parquet"),
                    events=result.events,
                )
            except Exception:
                # Parquet sink is best-effort and must never fail the run.
                pass
        persist_run_metrics(
            run_cfg.db_path,
            run_id=run_id,
            metrics=result.metrics,
            trial_id=trial_id,
            extra={
                "symbol": run_cfg.symbol,
                "interval": run_cfg.interval,
                "final_state": result.final_state,
            },
        )
        finish_run(run_cfg.db_path, run_id=run_id, status="completed")
        result.run_id = run_id
        return result
    except Exception:
        finish_run(run_cfg.db_path, run_id=run_id, status="failed")
        raise

