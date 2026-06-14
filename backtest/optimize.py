"""Optuna-based optimization for backtest strategies.

Fase 3 extends the original `optimize_strategy(...)` entry point with two
opt-in surfaces:

- An explicit `optuna_storage_url` keyword that, when omitted, is resolved via
  `backtest.optuna_storage.build_storage(study_name, app_config)`. This lets
  callers route Optuna to PostgreSQL (`optuna` schema) when `BACKTEST_METADATA_BACKEND=pg`
  while keeping the legacy SQLite path for callers that pass `optuna_storage_db`.
- An `orchestrator` keyword that, when provided, disables Optuna's own thread
  pool and dispatches trials externally via `backtest.orchestrator.Orchestrator`.

`optimize_strategy_parallel(...)` is a convenience wrapper that wires the
PostgreSQL-backed storage, builds an `Orchestrator`, and runs trials in
isolated subprocesses with crash-isolation and `ResourceGuard` throttling.

Default behaviour (no `orchestrator`, no `optuna_storage_url`, no
`app_config`) is byte-for-byte identical to the pre-Fase-3 behaviour so every
existing call site keeps working.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Type

try:
    import optuna  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - runtime guard
    optuna = None

from backtest.cleanup import abort_stale_runs
from backtest.config import AppConfig
from backtest.engine import EngineConfig
from backtest.registry import suggest_params
from backtest.runner import execute_and_persist
from backtest.storage import save_trial, save_trial_metrics
from backtest.strategy_base import StrategyBase
from db import init_db


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
    seed: int | None = None

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


def _build_sampler(sampler_name: str, seed: int | None):
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


def _resolve_storage_url(
    optuna_storage_url: str | None,
    optuna_storage_db: str | None,
    db_path: str,
    study_name: str,
    app_config: AppConfig | None,
) -> str:
    """Pick the first non-None of: explicit URL, legacy sqlite path, build_storage."""
    if optuna_storage_url:
        return optuna_storage_url
    if optuna_storage_db:
        return f"sqlite:///{optuna_storage_db}"
    if app_config is not None:
        from backtest.optuna_storage import build_storage

        return build_storage(study_name, app_config, sqlite_path=db_path)
    return f"sqlite:///{db_path}"


def optimize_strategy(
    db_path: str,
    study_name: str,
    strategy_cls: Type[StrategyBase],
    base_config: EngineConfig,
    trials: int = 50,
    n_jobs: int = 1,
    timeout: int | None = None,
    search_overrides: Dict[str, Any] | None = None,
    optimization: OptimizationConfig | None = None,
    events_mode: str | None = None,
    optuna_storage_db: str | None = None,
    *,
    optuna_storage_url: str | None = None,
    app_config: AppConfig | None = None,
    orchestrator: "object" | None = None,
) -> Any:
    """Run Optuna optimization, optionally with PG storage and an Orchestrator.

    Legacy behaviour is fully preserved when neither `optuna_storage_url`,
    `app_config`, nor `orchestrator` are provided:
    - `optuna_storage_db`, when set, keeps writing Optuna into its own SQLite
      file (eliminating contention with `bt_events`/`bt_runs` on `db_path`).
    - Otherwise Optuna lands in `sqlite:///<db_path>`, exactly like before.

    When `app_config` is supplied (without `optuna_storage_url`), the storage
    URL is resolved via `backtest.optuna_storage.build_storage(...)`, which
    routes to PostgreSQL when `metadata_backend == 'pg'`.

    When `orchestrator` is provided, Optuna's own thread-pool is disabled
    (n_jobs=1 inside `study.optimize`) and parallelism is controlled
    externally by `optimize_strategy_parallel(...)`.
    """
    if optuna is None:
        raise RuntimeError("Optuna is not installed. Run: pip install -r requirements.txt")
    init_db(db_path)
    abort_stale_runs(db_path)
    opt = optimization or OptimizationConfig()
    sampler = _build_sampler(opt.sampler, opt.seed)
    storage_url = _resolve_storage_url(
        optuna_storage_url,
        optuna_storage_db,
        db_path,
        study_name,
        app_config,
    )
    study = optuna.create_study(
        study_name=study_name,
        storage=storage_url,
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

    if orchestrator is not None:
        # Caller drives parallelism externally; Optuna's thread pool is muted.
        _dispatch_via_orchestrator(
            orchestrator=orchestrator,
            study=study,
            storage_url=storage_url,
            study_name=study_name,
            strategy_cls=strategy_cls,
            base_config=base_config,
            trials=int(trials),
            opt=opt,
            search_overrides=search_overrides,
            events_mode=effective_events_mode,
            db_path=db_path,
            timeout=timeout,
        )
        return study

    study.optimize(objective, n_trials=int(trials), n_jobs=int(n_jobs), timeout=timeout)
    return study


# --- Orchestrator-backed parallel runner -----------------------------------

def _run_one_trial(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Worker entry-point: dispatch a single Optuna trial in an isolated process.

    Each worker opens its own connection to the shared Optuna storage (PG or
    SQLite), `ask`s for a trial, runs the objective, and `tell`s the outcome.
    The function must live at module scope so it can be pickled for spawn.
    """
    import optuna  # local import keeps top-level import path light

    from backtest.engine import EngineConfig
    from backtest.registry import get_strategy, suggest_params
    from backtest.runner import execute_and_persist
    from backtest.storage import save_trial, save_trial_metrics

    storage_url = payload["storage_url"]
    study_name = payload["study_name"]
    strategy_name = payload["strategy_name"]
    base_config_dict = payload["base_config_dict"]
    db_path = payload["db_path"]
    objective_metric = payload["objective_metric"]
    direction = payload.get("direction", "maximize")
    events_mode = payload.get("events_mode", DEFAULT_OPTIMIZATION_EVENTS_MODE)
    search_overrides = payload.get("search_overrides")
    sampler_name = payload.get("sampler", "tpe")
    sampler_seed = payload.get("sampler_seed")

    strategy_cls = get_strategy(strategy_name)

    if sampler_name == "random":
        sampler = optuna.samplers.RandomSampler(seed=sampler_seed)
    else:
        sampler = optuna.samplers.TPESampler(seed=sampler_seed)

    study = optuna.load_study(study_name=study_name, storage=storage_url, sampler=sampler)
    trial = study.ask()
    started_at = _utc_now()

    params = suggest_params(trial, strategy_name, search_overrides=search_overrides)
    if params.get("_invalid"):
        study.tell(trial, state=optuna.trial.TrialState.PRUNED)
        return {"ok": True, "pruned": True, "trial_number": int(trial.number)}

    params = {k: v for k, v in params.items() if not k.startswith("_")}

    cfg = EngineConfig(**base_config_dict)
    cfg.events_mode = events_mode
    if strategy_name == "sma_cross":
        cfg.sma_fast = int(params["fast"])
        cfg.sma_slow = int(params["slow"])

    try:
        result = execute_and_persist(
            config=cfg,
            strategy_cls=strategy_cls,
            strategy_params=params,
        )
    except Exception as exc:
        study.tell(trial, state=optuna.trial.TrialState.FAIL)
        raise

    objective_value = _coerce_metric_value(result.metrics.get(objective_metric))
    study.tell(trial, objective_value)

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
        duration_sec=(
            datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
        ).total_seconds(),
    )
    save_trial_metrics(db_path=db_path, trial_id=trial_id, metrics=result.metrics)
    return {
        "ok": True,
        "trial_number": int(trial.number),
        "objective": float(objective_value),
        "run_id": (int(result.run_id) if result.run_id is not None else None),
    }


def _dispatch_via_orchestrator(
    *,
    orchestrator: Any,
    study: Any,
    storage_url: str,
    study_name: str,
    strategy_cls: Type[StrategyBase],
    base_config: EngineConfig,
    trials: int,
    opt: OptimizationConfig,
    search_overrides: Dict[str, Any] | None,
    events_mode: str,
    db_path: str,
    timeout: int | None,
) -> None:
    """Submit `trials` jobs to `orchestrator.map`, one isolated worker each."""
    payload_template: Dict[str, Any] = {
        "storage_url": storage_url,
        "study_name": study_name,
        "strategy_name": strategy_cls.name,
        "base_config_dict": dict(base_config.__dict__),
        "db_path": db_path,
        "objective_metric": opt.objective_metric,
        "direction": opt.direction,
        "sampler": opt.sampler,
        "sampler_seed": opt.seed,
        "events_mode": events_mode,
        "search_overrides": search_overrides,
    }
    jobs = [dict(payload_template) for _ in range(int(trials))]
    orchestrator.map(_run_one_trial, jobs)


def optimize_strategy_parallel(
    db_path: str,
    study_name: str,
    strategy_cls: Type[StrategyBase],
    base_config: EngineConfig,
    trials: int,
    n_jobs: int = 1,
    executor: str = "joblib",
    app_config: AppConfig | None = None,
    optimization: OptimizationConfig | None = None,
    search_overrides: Dict[str, Any] | None = None,
    events_mode: str = DEFAULT_OPTIMIZATION_EVENTS_MODE,
    ram_cap_pct: float = 80.0,
    cpu_cap_pct: float = 80.0,
    per_worker_ram_mb: int | None = None,
    per_trial_timeout_sec: int | None = None,
) -> Any:
    """High-level entry point: Orchestrator + ResourceGuard + Optuna PG storage.

    Builds the Optuna storage URL via `optuna_storage.build_storage(...)`,
    constructs an `Orchestrator` configured with the requested executor and
    concurrency caps, and dispatches `trials` jobs through it. Each trial
    runs in an isolated subprocess via `worker_isolation.spawn_isolated_worker`
    so a single OOM/timeout does not contaminate the search.
    """
    from backtest.optuna_storage import build_storage
    from backtest.orchestrator import Orchestrator, OrchestratorConfig

    cfg_app = app_config or AppConfig.from_env()
    storage_url = build_storage(study_name, cfg_app, sqlite_path=db_path)

    orch_cfg = OrchestratorConfig(
        executor=executor,
        n_jobs=int(n_jobs),
        ram_cap_pct=float(ram_cap_pct),
        cpu_cap_pct=float(cpu_cap_pct),
        per_worker_ram_mb=per_worker_ram_mb,
        per_trial_timeout_sec=per_trial_timeout_sec,
    )
    orchestrator = Orchestrator(orch_cfg)

    return optimize_strategy(
        db_path=db_path,
        study_name=study_name,
        strategy_cls=strategy_cls,
        base_config=base_config,
        trials=int(trials),
        n_jobs=1,
        timeout=None,
        search_overrides=search_overrides,
        optimization=optimization,
        events_mode=events_mode,
        optuna_storage_url=storage_url,
        app_config=cfg_app,
        orchestrator=orchestrator,
    )
