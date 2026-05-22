"""Optuna-based optimization for backtest strategies."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Type

try:
    import optuna  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - runtime guard
    optuna = None

from backtest.cleanup import abort_stale_runs
from backtest.engine import EngineConfig
from backtest.registry import suggest_params
from backtest.runner import execute_and_persist
from backtest.storage import save_trial, save_trial_metrics
from backtest.strategy_base import StrategyBase
from db import init_db


# Default to "lite" persistence during optimization so we don't blow up SQLite
# when running many trials in parallel on large datasets. Callers can override.
DEFAULT_OPTIMIZATION_EVENTS_MODE = "lite"


AVAILABLE_OBJECTIVE_METRICS = (
    "total_return",
    "final_equity",
    "sharpe",
    "sortino",
    "calmar",
    "ulcer_index",
    "profit_factor",
    "win_rate",
    "max_drawdown",
    "num_trades",
)

AVAILABLE_SAMPLERS = ("tpe", "random")


@dataclass
class OptimizationConfig:
    """Configuration knobs for an Optuna optimization run.

    Negative directions on metrics like `max_drawdown` should be combined with
    `direction='minimize'` to actually search for the smallest drawdown.
    """

    objective_metric: str = "total_return"
    direction: str = "maximize"
    sampler: str = "tpe"
    seed: Optional[int] = None

    def __post_init__(self) -> None:
        metric = (self.objective_metric or "total_return").strip().lower()
        if metric not in AVAILABLE_OBJECTIVE_METRICS:
            raise ValueError(
                f"Unsupported objective_metric '{metric}'. Available: {', '.join(AVAILABLE_OBJECTIVE_METRICS)}"
            )
        direction = (self.direction or "maximize").strip().lower()
        if direction not in ("maximize", "minimize"):
            raise ValueError(f"Unsupported direction '{direction}'. Use 'maximize' or 'minimize'.")
        sampler = (self.sampler or "tpe").strip().lower()
        if sampler not in AVAILABLE_SAMPLERS:
            raise ValueError(f"Unsupported sampler '{sampler}'. Available: {', '.join(AVAILABLE_SAMPLERS)}")
        self.objective_metric = metric
        self.direction = direction
        self.sampler = sampler


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_sampler(sampler_name: str, seed: Optional[int]):
    if optuna is None:  # pragma: no cover - runtime guard
        return None
    if sampler_name == "random":
        return optuna.samplers.RandomSampler(seed=seed)
    return optuna.samplers.TPESampler(seed=seed)


def _coerce_metric_value(value: Any) -> float:
    if value is None:
        return 0.0
    try:
        return float(value)
    except Exception:
        return 0.0


def optimize_strategy(
    db_path: str,
    study_name: str,
    strategy_cls: Type[StrategyBase],
    base_config: EngineConfig,
    trials: int = 50,
    n_jobs: int = 1,
    timeout: Optional[int] = None,
    search_overrides: Optional[Dict[str, Any]] = None,
    optimization: Optional[OptimizationConfig] = None,
    events_mode: Optional[str] = None,
    optuna_storage_db: Optional[str] = None,
) -> Any:
    """Run Optuna optimization with optional storage segregation.

    When `optuna_storage_db` is provided, the Optuna RDB storage is kept in a
    separate SQLite file (e.g. `runs/strict_x/optuna.db`) so write contention
    with our `bt_events`/`bt_runs` tables on `db_path` is eliminated.
    Defaults to legacy behaviour (Optuna stored on `db_path`).
    """
    if optuna is None:
        raise RuntimeError("Optuna is not installed. Run: pip install -r requirements.txt")
    init_db(db_path)
    abort_stale_runs(db_path)
    opt = optimization or OptimizationConfig()
    sampler = _build_sampler(opt.sampler, opt.seed)
    storage_path = optuna_storage_db or db_path
    study = optuna.create_study(
        study_name=study_name,
        storage=f"sqlite:///{storage_path}",
        load_if_exists=True,
        direction=opt.direction,
        sampler=sampler,
    )

    effective_events_mode = (events_mode or DEFAULT_OPTIMIZATION_EVENTS_MODE).strip().lower()

    def objective(trial: "optuna.Trial") -> float:
        started_at = _utc_now()
        params = suggest_params(trial, strategy_cls.name, search_overrides=search_overrides)
        if params.get("_invalid"):
            raise optuna.exceptions.TrialPruned()
        params = {k: v for k, v in params.items() if not k.startswith("_")}

        cfg = EngineConfig(**base_config.__dict__)
        cfg.events_mode = effective_events_mode
        if strategy_cls.name == "sma_cross":
            cfg.sma_fast = int(params["fast"])
            cfg.sma_slow = int(params["slow"])
        result = execute_and_persist(
            config=cfg,
            strategy_cls=strategy_cls,
            strategy_params=params,
        )
        objective_value = _coerce_metric_value(result.metrics.get(opt.objective_metric))
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
