"""Run a single strategy across a basket of symbols.

This module is the multi-asset companion to :mod:`backtest.walkforward_runner`.
Each symbol gets its own ``EngineConfig`` clone (independent bankroll) and is
backtested via the standard ``execute_and_persist`` path. A joint cash pool
across symbols is intentionally out of scope for Fase 4 and raises
``NotImplementedError`` so the CLI surface stays forward-compatible.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional

from backtest.aggregator import aggregate_multi_symbol_metrics
from backtest.engine import EngineConfig
from backtest.registry import get_strategy
from backtest.runner import execute_and_persist


@dataclass
class MultiSymbolConfig:
    symbols: List[str]
    interval: str
    start_ts: int | None
    end_ts: int | None
    initial_cash_per_symbol: float
    share_cash_pool: bool = False

    def __post_init__(self) -> None:
        symbols = [str(s).strip() for s in (self.symbols or []) if str(s).strip()]
        if not symbols:
            raise ValueError("MultiSymbolConfig.symbols must contain at least one symbol")
        # Preserve insertion order while removing duplicates.
        seen: Dict[str, None] = {}
        for sym in symbols:
            seen.setdefault(sym, None)
        self.symbols = list(seen.keys())
        if not str(self.interval).strip():
            raise ValueError("MultiSymbolConfig.interval must be non-empty")
        if float(self.initial_cash_per_symbol) <= 0.0:
            raise ValueError("initial_cash_per_symbol must be > 0")


@dataclass
class MultiSymbolResult:
    per_symbol: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    aggregated: Dict[str, Any] = field(default_factory=dict)


@dataclass
class _SymbolJob:
    symbol: str
    interval: str
    start_ts: int | None
    end_ts: int | None
    initial_cash: float
    strategy_name: str
    strategy_params: Dict[str, Any]
    engine_config: EngineConfig
    db_path: str


def _engine_cfg_for_symbol(job: _SymbolJob) -> EngineConfig:
    return replace(
        job.engine_config,
        db_path=job.db_path,
        symbol=job.symbol,
        interval=job.interval,
        start_ts=job.start_ts,
        end_ts=job.end_ts,
        initial_cash=float(job.initial_cash),
    )


def _run_symbol(job: _SymbolJob) -> Dict[str, Any]:
    strategy_cls = get_strategy(job.strategy_name)
    cfg = _engine_cfg_for_symbol(job)
    result = execute_and_persist(
        config=cfg,
        strategy_cls=strategy_cls,
        strategy_params=dict(job.strategy_params or {}),
    )
    return {
        "symbol": str(job.symbol),
        "run_id": result.run_id,
        "metrics": dict(result.metrics or {}),
        "params": dict(job.strategy_params or {}),
    }


def run_multi_symbol(
    cfg: MultiSymbolConfig,
    strategy_name: str,
    strategy_params: Dict[str, Any],
    engine_config: EngineConfig,
    db_path: str,
    *,
    orchestrator: Any | None = None,
    progress_cb: Callable[[Dict[str, Any]], None] | None = None,
) -> MultiSymbolResult:
    """Run ``strategy_name`` over each symbol in ``cfg`` and aggregate metrics."""
    if cfg.share_cash_pool:
        raise NotImplementedError(
            "joint-pool multi-symbol is reserved for a future phase"
        )

    jobs = [
        _SymbolJob(
            symbol=symbol,
            interval=cfg.interval,
            start_ts=cfg.start_ts,
            end_ts=cfg.end_ts,
            initial_cash=float(cfg.initial_cash_per_symbol),
            strategy_name=strategy_name,
            strategy_params=dict(strategy_params or {}),
            engine_config=engine_config,
            db_path=db_path,
        )
        for symbol in cfg.symbols
    ]

    per_symbol: Dict[str, Dict[str, Any]] = {}
    if orchestrator is not None:
        try:
            mapped = orchestrator.map(_run_symbol, jobs)
        except AttributeError as exc:
            raise RuntimeError(
                "orchestrator must expose a .map(fn, jobs) method"
            ) from exc
        for payload in mapped:
            per_symbol[str(payload["symbol"])] = payload
            if progress_cb is not None:
                try:
                    progress_cb({"event": "symbol_done", **payload})
                except Exception:
                    pass
    else:
        for job in jobs:
            payload = _run_symbol(job)
            per_symbol[str(payload["symbol"])] = payload
            if progress_cb is not None:
                try:
                    progress_cb({"event": "symbol_done", **payload})
                except Exception:
                    pass

    aggregated = aggregate_multi_symbol_metrics(per_symbol)
    return MultiSymbolResult(per_symbol=per_symbol, aggregated=aggregated)


__all__ = [
    "MultiSymbolConfig",
    "MultiSymbolResult",
    "run_multi_symbol",
]
