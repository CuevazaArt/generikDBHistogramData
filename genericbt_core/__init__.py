"""Python entry point for the Rust core.

This module is the canonical import surface for callers (sibling CLI sets
``BACKTEST_ENGINE_KIND=rust`` then ``import genericbt_core`` to dispatch
into the native loop). When the maturin-built ``_genericbt_core`` extension
is not on disk — typical for developer machines without a Rust toolchain —
``run_backtest`` transparently falls through to the pure-Python engine in
``backtest.engine`` so every test, script and CLI command keeps working.

Public surface kept intentionally tiny:

* :func:`run_backtest` — same signature/return type as
  :func:`backtest.engine.run_backtest`. Returns a
  :class:`backtest.engine.BacktestResult` either way.
* :func:`is_rust_available` — boolean; ``True`` iff the compiled extension
  was importable at module load time.
* :class:`EngineConfig`, :class:`BacktestResult` — re-exported from
  :mod:`backtest.engine` so callers can do ``from genericbt_core import
  EngineConfig`` without dragging in the rest of the package.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Type

_RUST_AVAILABLE: bool = False
_rust: Any = None
try:  # pragma: no cover - exercised in CI when the wheel is built.
    from . import _genericbt_core as _rust  # type: ignore[attr-defined]

    # The sibling stub directory ``_genericbt_core/__init__.pyi`` exists
    # purely for type checkers. Without a compiled ``.abi3.pyd``/``.so``
    # next to it, Python's namespace-package machinery would still resolve
    # the import to an empty module, which would falsely report Rust as
    # available. Guard by probing for the actual native ``run_backtest``.
    if hasattr(_rust, "run_backtest") and callable(getattr(_rust, "run_backtest", None)):
        _RUST_AVAILABLE = True
    else:
        _rust = None
        _RUST_AVAILABLE = False
except ImportError:
    _rust = None
    _RUST_AVAILABLE = False

from backtest.engine import BacktestResult, EngineConfig
from backtest.engine import run_backtest as _py_run_backtest
from backtest.strategy_base import StrategyBase

__all__ = [
    "run_backtest",
    "is_rust_available",
    "EngineConfig",
    "BacktestResult",
]


def is_rust_available() -> bool:
    """Return ``True`` iff the compiled ``_genericbt_core`` extension loaded.

    The CLI / orchestrator reads this to decide whether to surface the
    ``--engine=rust`` flag, and the parity test suite uses it to skip the
    Rust-vs-Python comparison when only the fallback is installed.
    """

    return _RUST_AVAILABLE


def _engine_kind() -> str:
    return os.getenv("BACKTEST_ENGINE_KIND", "python").strip().lower()


def _engine_config_to_dict(cfg: EngineConfig) -> Dict[str, Any]:
    """Marshal the ``EngineConfig`` dataclass into a plain dict consumable
    by the Rust ``run_backtest`` pyfunction.

    Keep this in sync with the fields the Rust side actually reads (see
    ``crates/genericbt-core/src/lib.rs::parse_engine_config``). Unknown
    keys are tolerated by the Rust side via ``serde(default)``.
    """

    return {
        "db_path": cfg.db_path,
        "symbol": cfg.symbol,
        "interval": cfg.interval,
        "start_ts": cfg.start_ts,
        "end_ts": cfg.end_ts,
        "initial_cash": float(cfg.initial_cash),
        "fee_rate": float(cfg.fee_rate),
        "slippage_bps": float(cfg.slippage_bps),
        "use_heikin_ashi": bool(cfg.use_heikin_ashi),
        "price_source": cfg.price_source,
        "sma_fast": int(cfg.sma_fast),
        "sma_slow": int(cfg.sma_slow),
        "ema_period": int(cfg.ema_period),
        "rsi_period": int(cfg.rsi_period),
        "atr_period": int(cfg.atr_period),
        "loop_seconds": cfg.loop_seconds,
        "events_mode": cfg.events_mode,
        "snapshot_seconds": int(cfg.snapshot_seconds),
        "initial_state": cfg.initial_state,
        # Fase 2: checkpoint knobs threaded through to the Rust core.
        # The Rust shim accepts unknown keys (serde(default)) so older
        # wheels remain forward-compatible.
        "checkpoint_every_bars": cfg.checkpoint_every_bars,
        "checkpoint_every_sim_seconds": cfg.checkpoint_every_sim_seconds,
        "checkpoints_dir": cfg.checkpoints_dir,
        "resume_from_checkpoint": cfg.resume_from_checkpoint,
    }


def _run_rust(
    config: EngineConfig,
    strategy_cls: Type[StrategyBase],
    strategy_params: Optional[Dict[str, Any]],
    run_id: Optional[int],
    trial_id: Optional[int],
    candles: Optional[List[Dict[str, Any]]],
) -> BacktestResult:
    """Rust dispatch path.

    The Rust core only owns the bar loop; all data acquisition and the
    candle pre-pass (Heikin-Ashi, ``price_source`` selection, indicator
    columns, custom SMA columns) still live in Python so we can share that
    code with the pure-Python fallback and avoid duplicating pyarrow / DB
    glue in the crate. Fase 2 will push the pre-pass into Rust on top of
    Arrow RecordBatch input.
    """

    from backtest.data_feed import candles_to_dicts, load_candles
    from backtest.engine import _add_custom_smas
    from backtest.indicators import apply_indicators
    from backtest.transforms import apply_candle_source, apply_heikin_ashi

    strategy_params = strategy_params or {}
    if candles is None:
        candles = candles_to_dicts(
            load_candles(
                config.db_path,
                symbol=config.symbol,
                interval=config.interval,
                start_ts=config.start_ts,
                end_ts=config.end_ts,
            )
        )
    if config.use_heikin_ashi:
        candles = apply_heikin_ashi(candles)
        source = "ha_close"
    else:
        source = config.price_source
    candles = apply_candle_source(candles, source=source)
    candles = apply_indicators(
        candles,
        sma_period=config.sma_fast,
        ema_period=config.ema_period,
        rsi_period=config.rsi_period,
        atr_period=config.atr_period,
        price_key="price_source",
    )
    _add_custom_smas(candles, config.sma_fast, config.sma_slow)

    strategy = strategy_cls(**strategy_params)
    strategy.on_start(candles)
    initial_state = config.initial_state if isinstance(config.initial_state, dict) else None
    strategy_seed = initial_state.get("strategy", {}) if initial_state else {}
    if isinstance(strategy_seed, dict):
        strategy.import_state(strategy_seed)

    cfg_dict = _engine_config_to_dict(config)
    result_dict = _rust.run_backtest(cfg_dict, strategy, candles, run_id, trial_id)

    return BacktestResult(
        config=config,
        metrics=dict(result_dict["metrics"]),
        events=list(result_dict["events"]),
        equity_curve=list(result_dict["equity_curve"]),
        candles=candles,
        run_id=result_dict.get("run_id", run_id),
        trial_id=result_dict.get("trial_id", trial_id),
        final_state=dict(result_dict["final_state"]),
    )


def run_backtest(
    config: EngineConfig,
    strategy_cls: Type[StrategyBase],
    strategy_params: Optional[Dict[str, Any]] = None,
    run_id: Optional[int] = None,
    trial_id: Optional[int] = None,
    candles: Optional[List[Dict[str, Any]]] = None,
) -> BacktestResult:
    """Execute a backtest, preferring the Rust core when available.

    Selection rule (matches ``backtest_cli.py --engine``):

    * If ``BACKTEST_ENGINE_KIND`` is ``"rust"`` *and* the compiled
      extension is importable, dispatch to :func:`_run_rust`.
    * Otherwise, call the unchanged pure-Python implementation from
      :mod:`backtest.engine`.

    Either path returns a :class:`backtest.engine.BacktestResult` with the
    same numerical metrics (within ~1e-12 by design; see
    ``tests/test_genericbt_core_parity.py``).
    """

    if _RUST_AVAILABLE and _engine_kind() == "rust":
        return _run_rust(
            config=config,
            strategy_cls=strategy_cls,
            strategy_params=strategy_params,
            run_id=run_id,
            trial_id=trial_id,
            candles=candles,
        )
    return _py_run_backtest(
        config=config,
        strategy_cls=strategy_cls,
        strategy_params=strategy_params,
        run_id=run_id,
        trial_id=trial_id,
        candles=candles,
    )
