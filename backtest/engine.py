"""Core backtesting engine."""
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple, Type

from backtest.broker import SpotBroker
from backtest.data_feed import candles_to_dicts, load_candles
from backtest.events import Event
from backtest.indicators import _rolling_sma, apply_indicators
from backtest.metrics import summarize_metrics
from backtest.strategy_base import StrategyBase, StrategyContext
from backtest.transforms import apply_candle_source, apply_heikin_ashi


_EVENTS_MODES = ("full", "lite", "minimal")

# Engine identity baked into every checkpoint. Bump when the loop semantics
# change in a way that would invalidate older checkpoints; the resume path
# can then refuse to load incompatible files.
ENGINE_KIND_PYTHON = "python"
ENGINE_VERSION = "0.2.0"


@dataclass
class EngineConfig:
    db_path: str
    symbol: str
    interval: str
    start_ts: Optional[int] = None
    end_ts: Optional[int] = None
    initial_cash: float = 10_000.0
    fee_rate: float = 0.001
    slippage_bps: float = 2.0
    use_heikin_ashi: bool = False
    price_source: str = "close"
    sma_fast: int = 10
    sma_slow: int = 30
    ema_period: int = 20
    rsi_period: int = 14
    atr_period: int = 14
    loop_seconds: Optional[int] = None
    # Persistence aggressiveness:
    #   "full"    -> emit one event per candle (legacy behaviour).
    #   "lite"    -> emit fills, rejects and periodic equity snapshots only.
    #   "minimal" -> emit fills and rejects only (no snapshots).
    events_mode: str = "full"
    # Used only in "lite" mode: minimum seconds between equity snapshots.
    snapshot_seconds: int = 3600
    # Optional warm-start snapshot for broker/strategy state.
    initial_state: Optional[Dict[str, Any]] = None
    # --- Fase 2: checkpointing knobs (all None preserves the legacy fast path) ---
    # Emit a checkpoint every N processed bars (None disables bar-based triggering).
    checkpoint_every_bars: Optional[int] = None
    # Emit a checkpoint every N seconds of simulated time elapsed
    # (None disables time-based triggering). Both triggers may run; we
    # emit whenever either threshold fires.
    checkpoint_every_sim_seconds: Optional[int] = None
    # Filesystem directory that will receive ``cp_<sim_ts>.json`` files.
    # Required when any ``checkpoint_every_*`` is set; otherwise ignored.
    checkpoints_dir: Optional[str] = None
    # Absolute path to a checkpoint file to resume from. When set, the
    # engine restores broker/strategy state, replays ``seq`` / clamps, and
    # skips candles up to (and including) ``candle_offset``.
    resume_from_checkpoint: Optional[str] = None


@dataclass
class BacktestResult:
    config: EngineConfig
    metrics: Dict[str, float]
    events: List[Dict]
    equity_curve: List[float]
    candles: List[Dict]
    run_id: Optional[int] = None
    trial_id: Optional[int] = None
    final_state: Dict[str, Any] = field(default_factory=dict)


def _add_custom_smas(candles: List[Dict], fast: int, slow: int) -> None:
    """Compute named SMA columns without cloning the candle list twice.

    Previously this triplicated the candle list (a fast clone + a slow clone)
    just to reuse `apply_indicators`. For 1s annual data that was tens of
    millions of dicts copied per worker. We now extract the price series once
    and compute both SMAs in a single pass via the same rolling helper used
    by `apply_indicators`, then write the results directly back onto the
    original candle dicts.
    """
    if not candles:
        return
    prices = [float(c.get("price_source", c["close"])) for c in candles]
    fast_vals = _rolling_sma(prices, fast)
    slow_vals = _rolling_sma(prices, slow)
    fast_key = f"sma_{fast}"
    slow_key = f"sma_{slow}"
    for i, c in enumerate(candles):
        c[fast_key] = fast_vals[i]
        c[slow_key] = slow_vals[i]


def run_backtest(
    config: EngineConfig,
    strategy_cls: Type[StrategyBase],
    strategy_params: Optional[Dict] = None,
    run_id: Optional[int] = None,
    trial_id: Optional[int] = None,
    candles: Optional[List[Dict]] = None,
) -> BacktestResult:
    """Execute a single backtest.

    If `candles` is provided, the engine skips DB loading and uses the
    supplied list as-is. This lets orchestrators chunk the data externally
    (see `backtest.data_feed.iter_candles_chunked`) so RAM stays bounded
    per worker on very long windows.
    """
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

    broker = SpotBroker(
        initial_cash=config.initial_cash,
        fee_rate=config.fee_rate,
        slippage_bps=config.slippage_bps,
    )
    initial_state = config.initial_state if isinstance(config.initial_state, dict) else None
    broker_seed = initial_state.get("broker", {}) if initial_state else {}
    if isinstance(broker_seed, dict):
        broker.state.cash = float(broker_seed.get("cash", broker.state.cash))
        broker.state.position_qty = max(0.0, float(broker_seed.get("position_qty", broker.state.position_qty)))
        broker.state.avg_entry = max(0.0, float(broker_seed.get("avg_entry", broker.state.avg_entry)))
        if broker.state.position_qty <= 0:
            broker.state.avg_entry = 0.0

    strategy = strategy_cls(**strategy_params)
    strategy.on_start(candles)
    strategy_seed = initial_state.get("strategy", {}) if initial_state else {}
    if isinstance(strategy_seed, dict):
        strategy.import_state(strategy_seed)

    events: List[Dict] = []
    equity_curve: List[float] = []
    trade_pnls: List[float] = []
    seq = 0
    last_trade_entry: Optional[Tuple[float, float]] = None
    last_exec_ts: Optional[int] = None

    events_mode = (config.events_mode or "full").strip().lower()
    if events_mode not in _EVENTS_MODES:
        events_mode = "full"
    emit_holds = events_mode == "full"
    emit_snapshots = events_mode == "lite"
    snapshot_step_ms = max(1, int(config.snapshot_seconds)) * 1000
    last_snapshot_ts: Optional[int] = None

    # --- Fase 2: checkpoint state ----------------------------------------
    # Decoupled from the legacy fast path: when all of these are None the
    # branch below is never entered and the loop is byte-identical to the
    # pre-Fase-2 behaviour. The no-regression test pins that property.
    cp_every_bars = (
        int(config.checkpoint_every_bars)
        if config.checkpoint_every_bars is not None and int(config.checkpoint_every_bars) > 0
        else None
    )
    cp_every_sim_seconds = (
        int(config.checkpoint_every_sim_seconds)
        if config.checkpoint_every_sim_seconds is not None and int(config.checkpoint_every_sim_seconds) > 0
        else None
    )
    cp_dir = config.checkpoints_dir if (cp_every_bars or cp_every_sim_seconds) else None
    checkpointing_enabled = cp_dir is not None and (
        cp_every_bars is not None or cp_every_sim_seconds is not None
    )
    bars_since_cp = 0
    last_checkpoint_sim_ts: Optional[int] = None

    # --- Fase 2: resume support ------------------------------------------
    skip_until_index: int = -1
    if config.resume_from_checkpoint:
        # Local import keeps `backtest.checkpoint` out of the hot import
        # graph for callers that never resume.
        from backtest.checkpoint import read_checkpoint

        cp = read_checkpoint(config.resume_from_checkpoint)
        broker_state = cp.broker_state or {}
        broker.state.cash = float(broker_state.get("cash", broker.state.cash))
        broker.state.position_qty = max(
            0.0, float(broker_state.get("position_qty", broker.state.position_qty))
        )
        broker.state.avg_entry = max(
            0.0, float(broker_state.get("avg_entry", broker.state.avg_entry))
        )
        if broker.state.position_qty <= 0:
            broker.state.avg_entry = 0.0
        if isinstance(cp.strategy_state, dict):
            strategy.import_state(cp.strategy_state)
        seq = int(cp.seq)
        last_exec_ts = cp.last_exec_ts
        last_snapshot_ts = cp.last_snapshot_ts
        last_trade_entry = cp.last_trade_entry
        skip_until_index = int(cp.candle_offset)
        # Audit row in the events list so downstream readers can see the
        # resume point inline with the rest of the run history.
        next_idx = skip_until_index + 1
        if 0 <= next_idx < len(candles):
            resume_px = float(
                candles[next_idx].get("price_source", candles[next_idx]["close"])
            )
        else:
            resume_px = 0.0
        resume_equity = float(broker.mark_equity(resume_px))
        seq += 1
        events.append(
            Event(
                seq=seq,
                event_time=int(cp.sim_ts),
                event_type="resume",
                cash=float(broker.state.cash),
                equity=resume_equity,
                position_qty=float(broker.state.position_qty),
                payload={
                    "checkpoint_path": str(config.resume_from_checkpoint),
                    "candle_offset": int(cp.candle_offset),
                    "engine_kind": cp.engine_kind,
                    "engine_version": cp.engine_version,
                },
            ).to_record()
        )

    for i, candle in enumerate(candles):
        if skip_until_index >= 0 and i <= skip_until_index:
            continue
        candle_ts = int(candle["open_time"])
        if config.loop_seconds is not None and config.loop_seconds > 0 and last_exec_ts is not None:
            if candle_ts - last_exec_ts < int(config.loop_seconds) * 1000:
                continue
        last_exec_ts = candle_ts
        # --- Fase 2 checkpoint trigger ----------------------------------
        # Threshold evaluation happens BEFORE the bar's signal is generated
        # so the snapshot captures the broker/strategy state that has
        # already been persisted (i.e. fills from previous bars). On resume
        # we re-enter the loop at candle_offset + 1 with that exact state.
        if checkpointing_enabled:
            bars_since_cp += 1
            bar_due = cp_every_bars is not None and bars_since_cp >= cp_every_bars
            time_due = False
            if cp_every_sim_seconds is not None:
                if last_checkpoint_sim_ts is None:
                    # Use the first observed bar to anchor the time clock
                    # without emitting a checkpoint for it; otherwise we'd
                    # always write a no-op checkpoint at offset -1.
                    last_checkpoint_sim_ts = int(candle_ts)
                elif (candle_ts - last_checkpoint_sim_ts) >= int(cp_every_sim_seconds) * 1000:
                    time_due = True
            if bar_due or time_due:
                # Local import to avoid circular dependency at module load.
                from backtest.checkpoint import Checkpoint, write_checkpoint

                # i here is the 0-based offset of the bar we just decided
                # to process. We persist BEFORE the strategy runs, so on
                # resume we re-enter at i (the same bar) -- but we record
                # candle_offset = i - 1 so the resume slice
                # `candles[candle_offset + 1:]` reproduces this bar.
                offset = i - 1
                cp = Checkpoint(
                    run_id=int(run_id) if run_id is not None else -1,
                    sim_ts=int(candle_ts),
                    candle_offset=offset,
                    broker_state={
                        "cash": float(broker.state.cash),
                        "position_qty": float(broker.state.position_qty),
                        "avg_entry": float(broker.state.avg_entry),
                    },
                    strategy_state=strategy.export_state() or {},
                    seq=int(seq),
                    last_exec_ts=last_exec_ts,
                    last_snapshot_ts=last_snapshot_ts,
                    last_trade_entry=last_trade_entry,
                    engine_kind=ENGINE_KIND_PYTHON,
                    engine_version=ENGINE_VERSION,
                )
                target = os.path.join(cp_dir, f"cp_{int(candle_ts)}.json")
                write_checkpoint(target, cp)
                last_checkpoint_sim_ts = int(candle_ts)
                bars_since_cp = 0
        px = float(candle.get("price_source", candle["close"]))
        equity = broker.mark_equity(px)
        equity_curve.append(equity)

        ctx = StrategyContext(
            index=i,
            candle=candle,
            candles=candles,
            cash=broker.state.cash,
            position_qty=broker.state.position_qty,
            avg_entry=broker.state.avg_entry,
            equity=equity,
        )
        signal = strategy.on_bar(ctx)
        if signal.action in ("buy", "sell"):
            fill = broker.execute_market(signal.action, price=px, size_pct=signal.size_pct)
            seq += 1
            if fill:
                strategy.on_fill(fill=fill, signal=signal, ctx=ctx)
                event = Event(
                    seq=seq,
                    event_time=candle_ts,
                    event_type="fill",
                    side=fill["side"],
                    price=float(fill["price"]),
                    qty=float(fill["qty"]),
                    cash=float(broker.state.cash),
                    equity=float(broker.mark_equity(px)),
                    position_qty=float(broker.state.position_qty),
                    payload={"reason": signal.reason, "fee": float(fill["fee"]), **signal.metadata},
                    trial_id=trial_id,
                )
                events.append(event.to_record())
                if fill["side"] == "buy":
                    last_trade_entry = (float(fill["price"]), float(fill["qty"]))
                elif fill["side"] == "sell" and last_trade_entry is not None:
                    entry_price, entry_qty = last_trade_entry
                    qty = min(entry_qty, float(fill["qty"]))
                    trade_pnls.append((float(fill["price"]) - entry_price) * qty)
                    last_trade_entry = None
            else:
                event = Event(
                    seq=seq,
                    event_time=candle_ts,
                    event_type="order_rejected",
                    side=signal.action,
                    cash=float(broker.state.cash),
                    equity=float(broker.mark_equity(px)),
                    position_qty=float(broker.state.position_qty),
                    payload={"reason": signal.reason, **signal.metadata},
                    trial_id=trial_id,
                )
                events.append(event.to_record())
        else:
            if emit_holds:
                seq += 1
                events.append(
                    Event(
                        seq=seq,
                        event_time=candle_ts,
                        event_type="hold",
                        cash=float(broker.state.cash),
                        equity=float(equity),
                        position_qty=float(broker.state.position_qty),
                        payload={},
                        trial_id=trial_id,
                    ).to_record()
                )
            elif emit_snapshots and (
                last_snapshot_ts is None or (candle_ts - last_snapshot_ts) >= snapshot_step_ms
            ):
                seq += 1
                events.append(
                    Event(
                        seq=seq,
                        event_time=candle_ts,
                        event_type="snapshot",
                        cash=float(broker.state.cash),
                        equity=float(equity),
                        position_qty=float(broker.state.position_qty),
                        payload={},
                        trial_id=trial_id,
                    ).to_record()
                )
                last_snapshot_ts = candle_ts

    strategy.on_finish()
    final_px = float(candles[-1]["price_source"]) if candles else config.initial_cash
    final_equity = broker.mark_equity(final_px if candles else 0.0)
    metrics = summarize_metrics(
        initial_cash=config.initial_cash,
        final_equity=final_equity,
        equity_curve=equity_curve,
        trade_pnls=trade_pnls,
    )
    final_state = {
        "broker": {
            "cash": float(broker.state.cash),
            "position_qty": float(broker.state.position_qty),
            "avg_entry": float(broker.state.avg_entry),
        },
        "strategy": strategy.export_state(),
        "last_price": float(final_px if candles else 0.0),
        "final_equity": float(final_equity),
    }
    return BacktestResult(
        config=config,
        metrics=metrics,
        events=events,
        equity_curve=equity_curve,
        candles=candles,
        run_id=run_id,
        trial_id=trial_id,
        final_state=final_state,
    )


def run_backtest_streaming(
    config: EngineConfig,
    strategy_cls: Type[StrategyBase],
    strategy_params: Optional[Dict] = None,
    candle_batch_iter: Optional[Iterator[List[Dict]]] = None,
    run_id: Optional[int] = None,
    trial_id: Optional[int] = None,
) -> BacktestResult:
    """Run a backtest over an iterator of candle batches.

    This is the seam Fase 3 will pull against when wiring true Arrow-batch
    streaming through the engine. For now the implementation accumulates the
    yielded batches into a single in-memory ``candles`` list and delegates
    to :func:`run_backtest`, so the numerical output is byte-identical to the
    legacy code path (a `streaming_matches_in_memory` test pins that).

    Callers that need a streamed source should use
    :func:`backtest.data_feed.iter_candles_arrow_batches` (Parquet-backed)
    or any iterator that yields ``list[dict]``-shaped chunks.
    """
    if candle_batch_iter is None:
        # Caller wants the legacy load path; passing candles=None to
        # `run_backtest` triggers DB loading.
        return run_backtest(
            config=config,
            strategy_cls=strategy_cls,
            strategy_params=strategy_params,
            run_id=run_id,
            trial_id=trial_id,
            candles=None,
        )
    candles: List[Dict] = []
    for batch in candle_batch_iter:
        if not batch:
            continue
        candles.extend(batch)
    # When the iterator was explicitly provided, always honour it: pass
    # the accumulated list (possibly empty) so we never accidentally fall
    # back to DB loading on a zero-batch stream.
    return run_backtest(
        config=config,
        strategy_cls=strategy_cls,
        strategy_params=strategy_params,
        run_id=run_id,
        trial_id=trial_id,
        candles=candles,
    )
