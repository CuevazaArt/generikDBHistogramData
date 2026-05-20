"""Optuna-based optimization for backtest strategies."""
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Type

try:
    import optuna  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - runtime guard
    optuna = None

from backtest.engine import EngineConfig
from backtest.registry import suggest_params
from backtest.runner import execute_and_persist
from backtest.storage import save_trial, save_trial_metrics
from backtest.strategy_base import StrategyBase
from db import init_db


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def optimize_strategy(
    db_path: str,
    study_name: str,
    strategy_cls: Type[StrategyBase],
    base_config: EngineConfig,
    trials: int = 50,
    n_jobs: int = 1,
    timeout: Optional[int] = None,
    search_overrides: Optional[Dict[str, Any]] = None,
) -> Any:
    if optuna is None:
        raise RuntimeError("Optuna is not installed. Run: pip install -r requirements.txt")
    init_db(db_path)
    study = optuna.create_study(
        study_name=study_name,
        storage=f"sqlite:///{db_path}",
        load_if_exists=True,
        direction="maximize",
    )

    def objective(trial: optuna.Trial) -> float:
        started_at = _utc_now()
        params = suggest_params(trial, strategy_cls.name, search_overrides=search_overrides)
        if params.get("_invalid"):
            raise optuna.exceptions.TrialPruned()
        params = {k: v for k, v in params.items() if not k.startswith("_")}

        cfg = EngineConfig(**base_config.__dict__)
        if strategy_cls.name == "sma_cross":
            cfg.sma_fast = int(params["fast"])
            cfg.sma_slow = int(params["slow"])
        result = execute_and_persist(
            config=cfg,
            strategy_cls=strategy_cls,
            strategy_params=params,
        )
        objective_value = float(result.metrics["total_return"])
        finished_at = _utc_now()
        trial_id = save_trial(
            db_path=db_path,
            study_name=study_name,
            trial_number=trial.number,
            state="COMPLETE",
            objective=objective_value,
            params=params,
            started_at=started_at,
            finished_at=finished_at,
            duration_sec=(datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds(),
        )
        save_trial_metrics(db_path=db_path, trial_id=trial_id, metrics=result.metrics)
        return objective_value

    study.optimize(objective, n_trials=int(trials), n_jobs=int(n_jobs), timeout=timeout)
    return study

