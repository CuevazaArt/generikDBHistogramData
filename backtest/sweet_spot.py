"""Two-phase strategy search that converges on a "sweet" parameter setup.

Why two phases?
- Phase 1 (coarse): search broadly on a representative window with many
  parallel trials, "lite" persistence to keep SQLite from melting.
- Phase 2 (focused): replay the top-K candidates on the FULL window with
  "full" persistence, so the final report is rich and trustworthy.

The function returns a `SweetSpotResult` with everything the report generator
needs to interpret graphs and write the non-technical conclusion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Type

from backtest.cleanup import abort_stale_runs
from backtest.engine import EngineConfig
from backtest.guards import ResourceGuard, ResourceGuardConfig
from backtest.optimize import OptimizationConfig, optimize_strategy
from backtest.registry import get_strategy
from backtest.resources import recommend_n_jobs
from backtest.runner import execute_and_persist
from backtest.storage import save_trial, save_trial_metrics, study_trials
from backtest.strategy_base import StrategyBase


@dataclass
class SweetSpotConfig:
    db_path: str
    strategy_name: str
    symbol: str
    interval: str
    full_start_ts: int
    full_end_ts: int
    initial_cash: float = 10_000.0
    fee_rate: float = 0.001
    slippage_bps: float = 2.0
    use_heikin_ashi: bool = False
    loop_seconds: Optional[int] = None
    # Phase 1 (coarse search) controls
    coarse_window_pct: float = 0.25      # fraction of full window used for phase 1
    coarse_trials: int = 60
    coarse_mode: str = "balanced"         # resource mode for phase 1
    coarse_objective_metric: str = "total_return"
    coarse_direction: str = "maximize"
    coarse_sampler: str = "tpe"
    coarse_seed: Optional[int] = 42
    coarse_timeout: Optional[int] = None
    coarse_search_overrides: Dict[str, Any] = field(default_factory=dict)
    # Phase 2 (focused validation) controls
    focused_top_k: int = 5
    focused_mode: str = "safe"            # use few workers
    # Default to "lite" persistence: snapshots every `focused_snapshot_seconds`
    # keep equity-curve plots usable while avoiding tens of millions of "hold"
    # events on 1s datasets. Use "full" only on small datasets.
    focused_events_mode: str = "lite"
    focused_snapshot_seconds: int = 3600
    # Runtime guard defaults.
    guard_cpu_cap_pct: float = 90.0
    guard_ram_cap_pct: float = 90.0
    guard_sample_sec: float = 5.0
    guard_high_watermark_windows: int = 3
    guard_recover_windows: int = 3
    guard_backoff_sec: float = 10.0
    # Chunk coarse optimization into waves for adaptive n_jobs.
    coarse_wave_trials: int = 12
    # Optional split-storage layout for heavy parallel optimization:
    # when set, Optuna's RDB storage is isolated from `db_path`
    # (which keeps holding `klines` reads and `bt_events` writes).
    optuna_storage_db: Optional[str] = None


@dataclass
class SweetSpotResult:
    coarse_study_name: str
    focused_study_name: str
    coarse_trials_completed: int
    focused_runs: List[Dict[str, Any]]
    best_focused_run: Optional[Dict[str, Any]]
    coarse_window: Tuple[int, int]
    full_window: Tuple[int, int]
    config_snapshot: Dict[str, Any]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_engine_config(cfg: SweetSpotConfig, start_ts: int, end_ts: int, events_mode: str, snapshot_seconds: int) -> EngineConfig:
    return EngineConfig(
        db_path=cfg.db_path,
        symbol=cfg.symbol,
        interval=cfg.interval,
        start_ts=start_ts,
        end_ts=end_ts,
        initial_cash=cfg.initial_cash,
        fee_rate=cfg.fee_rate,
        slippage_bps=cfg.slippage_bps,
        use_heikin_ashi=cfg.use_heikin_ashi,
        loop_seconds=cfg.loop_seconds,
        events_mode=events_mode,
        snapshot_seconds=snapshot_seconds,
    )


def _coarse_window(full_start: int, full_end: int, pct: float) -> Tuple[int, int]:
    pct = max(0.05, min(1.0, float(pct)))
    span = max(1, full_end - full_start)
    coarse_span = int(span * pct)
    coarse_start = full_end - coarse_span
    return coarse_start, full_end


def run_sweet_spot_search(
    cfg: SweetSpotConfig,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> SweetSpotResult:
    """Execute the coarse search and the focused validation."""

    def _log(msg: str) -> None:
        if progress_cb is not None:
            progress_cb(msg)

    abort_stale_runs(cfg.db_path)
    strategy_cls: Type[StrategyBase] = get_strategy(cfg.strategy_name)
    guard = ResourceGuard(
        ResourceGuardConfig(
            cpu_cap_pct=float(cfg.guard_cpu_cap_pct),
            ram_cap_pct=float(cfg.guard_ram_cap_pct),
            sample_sec=float(cfg.guard_sample_sec),
            high_watermark_windows=int(cfg.guard_high_watermark_windows),
            recover_windows=int(cfg.guard_recover_windows),
        )
    )

    # ----- Phase 1: coarse search -----
    coarse_start, coarse_end = _coarse_window(cfg.full_start_ts, cfg.full_end_ts, cfg.coarse_window_pct)
    coarse_engine = _build_engine_config(cfg, coarse_start, coarse_end, events_mode="lite", snapshot_seconds=cfg.focused_snapshot_seconds)
    coarse_jobs = max(1, recommend_n_jobs(cfg.coarse_mode))
    # Ramp workers progressively across waves instead of starting at peak.
    dynamic_jobs = 1
    coarse_study_name = f"sweet_{cfg.strategy_name}_{cfg.symbol}_{cfg.interval}_coarse_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    _log(
        f"[phase1] coarse window {coarse_start}->{coarse_end} | "
        f"trials={cfg.coarse_trials} | n_jobs_base={coarse_jobs} | study={coarse_study_name}"
    )
    remaining_trials = int(cfg.coarse_trials)
    wave = 0
    while remaining_trials > 0:
        wave += 1
        snap = guard.snapshot()
        dynamic_jobs = min(coarse_jobs, max(1, guard.suggest_concurrency(dynamic_jobs, min=1)))
        if snap.get("throttle_active"):
            dynamic_jobs = max(1, min(dynamic_jobs, coarse_jobs // 2 or 1))
            _log(
                "[guard] throttle active "
                f"(cpu={snap.get('cpu_pct')} ram={snap.get('ram_pct')}) -> "
                f"n_jobs={dynamic_jobs}, backoff={cfg.guard_backoff_sec}s"
            )
            time.sleep(max(1.0, float(cfg.guard_backoff_sec)))
        for event in guard.consume_events():
            _log(f"[guard-event] {event.get('event')} | snapshot={event.get('snapshot')}")

        wave_trials = min(remaining_trials, max(1, int(cfg.coarse_wave_trials)))
        _log(f"[phase1-wave] wave={wave} trials={wave_trials} n_jobs={dynamic_jobs}")
        optimize_strategy(
            db_path=cfg.db_path,
            study_name=coarse_study_name,
            strategy_cls=strategy_cls,
            base_config=coarse_engine,
            trials=wave_trials,
            n_jobs=dynamic_jobs,
            timeout=cfg.coarse_timeout,
            search_overrides=cfg.coarse_search_overrides or None,
            optimization=OptimizationConfig(
                objective_metric=cfg.coarse_objective_metric,
                direction=cfg.coarse_direction,
                sampler=cfg.coarse_sampler,
                seed=cfg.coarse_seed,
            ),
            events_mode="lite",
            optuna_storage_db=cfg.optuna_storage_db,
        )
        remaining_trials -= wave_trials

    # ----- Phase 2: focused validation on top-K -----
    rows = study_trials(cfg.db_path, study_name=coarse_study_name, limit=2000)
    # Each row: (trial_id, trial_number, state, objective, params_json, started_at, finished_at, duration_sec)
    valid = [r for r in rows if r[3] is not None]
    if cfg.coarse_direction == "maximize":
        valid.sort(key=lambda r: float(r[3]), reverse=True)
    else:
        valid.sort(key=lambda r: float(r[3]))
    top = valid[: max(1, int(cfg.focused_top_k))]
    _log(f"[phase1-done] valid_trials={len(valid)} | top_k={len(top)}")

    focused_study_name = coarse_study_name.replace("_coarse_", "_focused_")
    focused_runs: List[Dict[str, Any]] = []
    full_engine = _build_engine_config(
        cfg,
        cfg.full_start_ts,
        cfg.full_end_ts,
        events_mode=cfg.focused_events_mode,
        snapshot_seconds=cfg.focused_snapshot_seconds,
    )

    for rank, row in enumerate(top, start=1):
        import json as _json

        try:
            params = _json.loads(str(row[4]) or "{}")
        except Exception:
            params = {}
        params = {k: v for k, v in params.items() if not k.startswith("_")}
        if strategy_cls.name == "sma_cross":
            full_engine.sma_fast = int(params.get("fast", full_engine.sma_fast))
            full_engine.sma_slow = int(params.get("slow", full_engine.sma_slow))
        _log(f"[phase2] focused rank={rank} | params={params}")
        started_at = _now_iso()
        result = execute_and_persist(
            config=full_engine,
            strategy_cls=strategy_cls,
            strategy_params=params,
        )
        finished_at = _now_iso()
        trial_id = save_trial(
            db_path=cfg.db_path,
            study_name=focused_study_name,
            trial_number=rank,
            state="COMPLETE",
            objective=float(result.metrics.get(cfg.coarse_objective_metric, 0.0) or 0.0),
            params=params,
            started_at=started_at,
            finished_at=finished_at,
            duration_sec=(datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds(),
        )
        save_trial_metrics(db_path=cfg.db_path, trial_id=trial_id, metrics=result.metrics)
        focused_runs.append(
            {
                "rank": rank,
                "run_id": result.run_id,
                "trial_id": trial_id,
                "params": params,
                "metrics": result.metrics,
            }
        )

    # Pick best by the configured objective on the FULL window.
    if focused_runs:
        if cfg.coarse_direction == "maximize":
            focused_runs.sort(key=lambda r: float(r["metrics"].get(cfg.coarse_objective_metric, 0.0) or 0.0), reverse=True)
        else:
            focused_runs.sort(key=lambda r: float(r["metrics"].get(cfg.coarse_objective_metric, 0.0) or 0.0))
        best = focused_runs[0]
    else:
        best = None

    return SweetSpotResult(
        coarse_study_name=coarse_study_name,
        focused_study_name=focused_study_name,
        coarse_trials_completed=len(valid),
        focused_runs=focused_runs,
        best_focused_run=best,
        coarse_window=(coarse_start, coarse_end),
        full_window=(cfg.full_start_ts, cfg.full_end_ts),
        config_snapshot={
            "strategy": cfg.strategy_name,
            "symbol": cfg.symbol,
            "interval": cfg.interval,
            "loop_seconds": cfg.loop_seconds,
            "coarse_window_pct": cfg.coarse_window_pct,
            "coarse_trials": cfg.coarse_trials,
            "focused_top_k": cfg.focused_top_k,
            "coarse_mode": cfg.coarse_mode,
            "focused_mode": cfg.focused_mode,
            "objective_metric": cfg.coarse_objective_metric,
            "resource_guard": {
                "cpu_cap_pct": cfg.guard_cpu_cap_pct,
                "ram_cap_pct": cfg.guard_ram_cap_pct,
                "sample_sec": cfg.guard_sample_sec,
                "high_watermark_windows": cfg.guard_high_watermark_windows,
                "recover_windows": cfg.guard_recover_windows,
                "backoff_sec": cfg.guard_backoff_sec,
            },
        },
    )
