"""Core backtesting engine."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Type

from backtest.broker import SpotBroker
from backtest.data_feed import candles_to_dicts, load_candles
from backtest.events import Event
from backtest.indicators import apply_indicators
from backtest.metrics import summarize_metrics
from backtest.strategy_base import StrategyBase, StrategyContext
from backtest.transforms import apply_candle_source, apply_heikin_ashi


_EVENTS_MODES = ("full", "lite", "minimal")


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


@dataclass
class BacktestResult:
    config: EngineConfig
    metrics: Dict[str, float]
    events: List[Dict]
    equity_curve: List[float]
    candles: List[Dict]
    run_id: Optional[int] = None
    trial_id: Optional[int] = None


def _add_custom_smas(candles: List[Dict], fast: int, slow: int) -> None:
    # Reuse indicator pipe: run two passes and store named columns.
    tmp_fast = [dict(c) for c in candles]
    apply_indicators(tmp_fast, sma_period=fast, ema_period=fast, rsi_period=14, atr_period=14, price_key="price_source")
    tmp_slow = [dict(c) for c in candles]
    apply_indicators(tmp_slow, sma_period=slow, ema_period=slow, rsi_period=14, atr_period=14, price_key="price_source")
    for i in range(len(candles)):
        candles[i][f"sma_{fast}"] = tmp_fast[i].get("sma")
        candles[i][f"sma_{slow}"] = tmp_slow[i].get("sma")


def run_backtest(
    config: EngineConfig,
    strategy_cls: Type[StrategyBase],
    strategy_params: Optional[Dict] = None,
    run_id: Optional[int] = None,
    trial_id: Optional[int] = None,
) -> BacktestResult:
    strategy_params = strategy_params or {}
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
    strategy = strategy_cls(**strategy_params)
    strategy.on_start(candles)

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

    for i, candle in enumerate(candles):
        candle_ts = int(candle["open_time"])
        if config.loop_seconds is not None and config.loop_seconds > 0 and last_exec_ts is not None:
            if candle_ts - last_exec_ts < int(config.loop_seconds) * 1000:
                continue
        last_exec_ts = candle_ts
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
    return BacktestResult(
        config=config,
        metrics=metrics,
        events=events,
        equity_curve=equity_curve,
        candles=candles,
        run_id=run_id,
        trial_id=trial_id,
    )

