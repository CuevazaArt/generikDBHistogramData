"""Rolling walk-forward evaluation primitives.

The legacy single-split helper :mod:`backtest.walkforward` is kept untouched for
backward compatibility. This module wraps the same building blocks
(``execute_and_persist`` + optional ``optimize_strategy``) into a multi-fold
loop with optional parallel dispatch via a Fase 3 orchestrator.

Folds use half-open semantics: ``test_start_ts == train_end_ts`` so the two
windows touch but never overlap. When the windows are forwarded to
``EngineConfig`` we subtract 1 ms from the upper bound to match the inclusive
``open_time <= end_ts`` predicate used by ``db.query_klines``.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

from backtest.aggregator import aggregate_walk_forward_metrics
from backtest.engine import EngineConfig
from backtest.registry import get_strategy
from backtest.runner import execute_and_persist


@dataclass(frozen=True)
class WalkForwardWindow:
    train_start_ts: int
    train_end_ts: int
    test_start_ts: int
    test_end_ts: int
    fold_index: int


@dataclass
class WalkForwardConfig:
    full_start_ts: int
    full_end_ts: int
    train_window_ms: int
    test_window_ms: int
    step_ms: int
    anchored: bool = False

    def __post_init__(self) -> None:
        if self.full_end_ts <= self.full_start_ts:
            raise ValueError("full_end_ts must be greater than full_start_ts")
        if self.train_window_ms <= 0:
            raise ValueError("train_window_ms must be > 0")
        if self.test_window_ms <= 0:
            raise ValueError("test_window_ms must be > 0")
        if self.step_ms <= 0:
            raise ValueError("step_ms must be > 0")


@dataclass
class WalkForwardResult:
    windows: List[WalkForwardWindow]
    fold_results: List[Dict[str, Any]] = field(default_factory=list)
    aggregated: Dict[str, Any] = field(default_factory=dict)


def build_windows(cfg: WalkForwardConfig) -> List[WalkForwardWindow]:
    """Materialize the ordered list of train/test windows for ``cfg``.

    For the rolling case (``cfg.anchored=False``), fold ``k`` has::

        train: [full_start + k*step, full_start + k*step + train_window)
        test:  [train_end,           train_end + test_window)

    For the anchored / expanding case, ``train_start`` is pinned at
    ``full_start_ts`` and the train window grows by ``step_ms`` per fold; the
    test window slides as before.
    """
    windows: List[WalkForwardWindow] = []
    full_start = int(cfg.full_start_ts)
    full_end = int(cfg.full_end_ts)
    train_w = int(cfg.train_window_ms)
    test_w = int(cfg.test_window_ms)
    step = int(cfg.step_ms)

    fold = 0
    while True:
        if cfg.anchored:
            train_start = full_start
            train_end = full_start + train_w + fold * step
        else:
            train_start = full_start + fold * step
            train_end = train_start + train_w
        test_start = train_end
        test_end = test_start + test_w
        if test_end > full_end:
            break
        if train_end <= train_start:
            break
        windows.append(
            WalkForwardWindow(
                train_start_ts=int(train_start),
                train_end_ts=int(train_end),
                test_start_ts=int(test_start),
                test_end_ts=int(test_end),
                fold_index=fold,
            )
        )
        fold += 1
    return windows


def _engine_cfg_for_window(
    base: EngineConfig,
    db_path: str,
    start_ts: int,
    end_ts: int,
) -> EngineConfig:
    """Return a clone of ``base`` restricted to the given timestamp window."""
    # `end_ts - 1` because query_klines uses `open_time <= end_ts` and our
    # window upper bound is exclusive (test_start_ts == train_end_ts).
    return replace(
        base,
        db_path=db_path,
        start_ts=int(start_ts),
        end_ts=int(end_ts) - 1,
    )


@dataclass
class _FoldJob:
    """All inputs needed by ``_run_fold`` to be pickled across processes."""

    fold_index: int
    train_start_ts: int
    train_end_ts: int
    test_start_ts: int
    test_end_ts: int
    strategy_name: str
    base_strategy_params: Dict[str, Any]
    engine_config: EngineConfig
    db_path: str
    optimize_per_fold: bool
    optimization_kwargs: Optional[Dict[str, Any]]


def _run_optuna_for_fold(
    *,
    strategy_name: str,
    train_cfg: EngineConfig,
    db_path: str,
    optimization_kwargs: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Lazy-import optimize_strategy so the module loads without optuna."""
    from backtest.optimize import optimize_strategy  # local import

    kwargs = dict(optimization_kwargs or {})
    trials = int(kwargs.pop("trials", 30))
    n_jobs = int(kwargs.pop("n_jobs", 1))
    timeout = kwargs.pop("timeout", None)
    study_name = str(
        kwargs.pop(
            "study_name",
            f"wf_{strategy_name}_{train_cfg.symbol}_{int(train_cfg.start_ts or 0)}",
        )
    )
    strategy_cls = get_strategy(strategy_name)
    study = optimize_strategy(
        db_path=db_path,
        study_name=study_name,
        strategy_cls=strategy_cls,
        base_config=train_cfg,
        trials=trials,
        n_jobs=n_jobs,
        timeout=timeout,
        **kwargs,
    )
    return dict(study.best_params)


def _run_fold(job: _FoldJob) -> Dict[str, Any]:
    """Execute a single train + test pair and return the aggregated payload."""
    strategy_cls = get_strategy(job.strategy_name)
    train_cfg = _engine_cfg_for_window(
        job.engine_config, job.db_path, job.train_start_ts, job.train_end_ts
    )

    if job.optimize_per_fold:
        params_for_test = _run_optuna_for_fold(
            strategy_name=job.strategy_name,
            train_cfg=train_cfg,
            db_path=job.db_path,
            optimization_kwargs=job.optimization_kwargs,
        )
    else:
        params_for_test = dict(job.base_strategy_params)

    train_result = execute_and_persist(
        config=train_cfg,
        strategy_cls=strategy_cls,
        strategy_params=params_for_test,
    )

    test_cfg = _engine_cfg_for_window(
        job.engine_config, job.db_path, job.test_start_ts, job.test_end_ts
    )
    test_result = execute_and_persist(
        config=test_cfg,
        strategy_cls=strategy_cls,
        strategy_params=params_for_test,
    )

    return {
        "fold_index": int(job.fold_index),
        "train_window": (int(job.train_start_ts), int(job.train_end_ts)),
        "test_window": (int(job.test_start_ts), int(job.test_end_ts)),
        "params": dict(params_for_test),
        "train_run_id": train_result.run_id,
        "test_run_id": test_result.run_id,
        "train_metrics": dict(train_result.metrics or {}),
        "test_metrics": dict(test_result.metrics or {}),
    }


def run_walk_forward(
    cfg: WalkForwardConfig,
    strategy_name: str,
    strategy_params: Dict[str, Any],
    engine_config: EngineConfig,
    db_path: str,
    *,
    optimize_per_fold: bool = False,
    optimization_kwargs: Optional[Dict[str, Any]] = None,
    orchestrator: Optional[Any] = None,
    progress_cb: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> WalkForwardResult:
    """Run all folds in ``cfg`` and aggregate the resulting metrics.

    When ``orchestrator`` is provided, fold dispatch is delegated to its
    ``map(callable, jobs)`` method (Fase 3). When omitted, folds are executed
    serially in the calling process and the function still produces fully
    persisted runs via ``execute_and_persist``.
    """
    windows = build_windows(cfg)
    if not windows:
        empty_result = WalkForwardResult(
            windows=[], fold_results=[], aggregated=aggregate_walk_forward_metrics([])
        )
        return empty_result

    jobs = [
        _FoldJob(
            fold_index=w.fold_index,
            train_start_ts=w.train_start_ts,
            train_end_ts=w.train_end_ts,
            test_start_ts=w.test_start_ts,
            test_end_ts=w.test_end_ts,
            strategy_name=strategy_name,
            base_strategy_params=dict(strategy_params or {}),
            engine_config=engine_config,
            db_path=db_path,
            optimize_per_fold=bool(optimize_per_fold),
            optimization_kwargs=dict(optimization_kwargs or {}) if optimization_kwargs else None,
        )
        for w in windows
    ]

    fold_results: List[Dict[str, Any]] = []
    if orchestrator is not None:
        # Fase 3 orchestrator: any object exposing a `.map(fn, iterable)` API.
        try:
            mapped = orchestrator.map(_run_fold, jobs)
        except AttributeError as exc:
            raise RuntimeError(
                "orchestrator must expose a .map(fn, jobs) method"
            ) from exc
        for payload in mapped:
            fold_results.append(payload)
            if progress_cb is not None:
                try:
                    progress_cb({"event": "fold_done", **payload})
                except Exception:
                    pass
    else:
        for job in jobs:
            payload = _run_fold(job)
            fold_results.append(payload)
            if progress_cb is not None:
                try:
                    progress_cb({"event": "fold_done", **payload})
                except Exception:
                    pass

    fold_results.sort(key=lambda d: int(d.get("fold_index", 0)))
    aggregated = aggregate_walk_forward_metrics(fold_results)
    return WalkForwardResult(windows=windows, fold_results=fold_results, aggregated=aggregated)


__all__ = [
    "WalkForwardWindow",
    "WalkForwardConfig",
    "WalkForwardResult",
    "build_windows",
    "run_walk_forward",
]
