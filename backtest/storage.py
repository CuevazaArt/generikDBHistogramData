"""Persistence layer for backtest runs, events, metrics and trials."""
from typing import Any, Dict, List, Optional, Tuple

from db import (
    create_bt_run,
    create_bt_trial,
    finish_bt_run,
    get_bt_equity_curve,
    get_bt_recent_events,
    get_bt_run_descriptor,
    get_bt_run_events,
    get_bt_run_metrics,
    get_bt_signal_events,
    get_bt_study_trials,
    get_bt_trial_objectives,
    insert_bt_events,
    list_bt_runs,
    list_top_bt_trials,
    upsert_bt_metrics,
    upsert_bt_trial_metrics,
)


def create_run(
    db_path: str,
    strategy_name: str,
    symbol: str,
    interval: str,
    start_ts: int | None,
    end_ts: int | None,
    initial_cash: float,
    fee_rate: float,
    slippage_bps: float,
    config: Dict[str, Any] | None = None,
) -> int:
    return create_bt_run(
        db_path,
        strategy_name=strategy_name,
        symbol=symbol,
        interval=interval,
        start_ts=start_ts,
        end_ts=end_ts,
        initial_cash=initial_cash,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        config=config or {},
    )


def finish_run(db_path: str, run_id: int, status: str = "completed") -> None:
    finish_bt_run(db_path, run_id=run_id, status=status)


def persist_run_events(
    db_path: str,
    run_id: int,
    events: List[Dict[str, Any]],
    batch_size: int = 5000,
) -> None:
    insert_bt_events(db_path, run_id=run_id, events=events, batch_size=batch_size)


def persist_run_metrics(
    db_path: str,
    run_id: int,
    metrics: Dict[str, float],
    trial_id: int | None = None,
    extra: Dict[str, Any] | None = None,
) -> None:
    upsert_bt_metrics(
        db_path,
        run_id=run_id,
        metrics=metrics,
        trial_id=trial_id,
        extra=extra or {},
    )


def summarize_run(db_path: str, run_id: int, events_limit: int = 25) -> Dict[str, Any]:
    return {
        "run_id": run_id,
        "metrics": get_bt_run_metrics(db_path, run_id=run_id),
        "recent_events": get_bt_recent_events(db_path, run_id=run_id, limit=events_limit),
    }


def list_runs(db_path: str, limit: int = 20) -> List[Tuple]:
    return list_bt_runs(db_path, limit=limit)


def save_trial(
    db_path: str,
    study_name: str,
    trial_number: int,
    state: str,
    objective: float | None,
    params: Dict[str, Any],
    started_at: str,
    finished_at: str | None,
    duration_sec: float | None,
) -> int:
    return create_bt_trial(
        db_path,
        study_name=study_name,
        trial_number=trial_number,
        state=state,
        objective=objective,
        params=params,
        started_at=started_at,
        finished_at=finished_at,
        duration_sec=duration_sec,
    )


def save_trial_metrics(db_path: str, trial_id: int, metrics: Dict[str, float]) -> None:
    upsert_bt_trial_metrics(db_path, trial_id=trial_id, metrics=metrics)


def top_trials(db_path: str, study_name: str, limit: int = 10) -> List[Tuple]:
    return list_top_bt_trials(db_path, study_name=study_name, limit=limit)


def run_equity_curve(db_path: str, run_id: int) -> List[Tuple]:
    return get_bt_equity_curve(db_path, run_id=run_id)


def trial_objectives(db_path: str, study_name: str, limit: int = 500) -> List[Tuple]:
    return get_bt_trial_objectives(db_path, study_name=study_name, limit=limit)


def run_signal_events(db_path: str, run_id: int) -> List[Tuple]:
    return get_bt_signal_events(db_path, run_id=run_id)


def run_descriptor(db_path: str, run_id: int) -> Dict[str, Any] | None:
    return get_bt_run_descriptor(db_path, run_id=run_id)


def run_events(db_path: str, run_id: int) -> List[Tuple]:
    return get_bt_run_events(db_path, run_id=run_id)


def study_trials(db_path: str, study_name: str, limit: int = 1000) -> List[Tuple]:
    return get_bt_study_trials(db_path, study_name=study_name, limit=limit)

