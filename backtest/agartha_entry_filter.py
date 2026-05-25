"""Agartha entry filter: detect favorable arming moment for the bot.

Module pure side-effect-free: given the recent price history (any iterable
of candles with open/high/low/close), evaluate whether NOW is a "good entry
moment" for the moonshot trailing strategy. Returns a decision dict with
the reason and the gate values, suitable for live monitoring (WS-driven)
or for backtest pre-arming logic.

Three-layer gate design (see ALPHA_STUDY_MODEL.md decision):

  Layer 1 (trigger):  Donchian low(N) - price must touch the rolling
                      minimum low of the last N bars on the operating
                      timeframe (default N=20 on 15m = 5h context).

  Layer 2 (macro):    Higher-timeframe MA filter - price must NOT be
                      collapsing under the higher-TF moving average by
                      more than `macro_drop_pct` (default 30%). Avoids
                      catching a knife in a clear macro dump.

  Layer 3 (momentum): Confirmation tick - the last bar must close above
                      the previous one (or last HA candle is bullish).
                      Confirms reversal has at least started.

ARMED = Layer1 AND Layer2 AND Layer3

User original idea (`MM(3, low) en 4h`) is a less strict form of Layer 2
that does NOT include Layer 1 (Donchian) and Layer 3 (uptick). Empirically
weaker because:
  - MM(3) is reactive: in a free-fall, the MA falls with price and the
    bot keeps buying as it cracks.
  - No uptick filter -> buys mid-crash.
This module retains the macro MA idea but uses MM(N, source) with N
configurable and adds the other two layers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional, Sequence, Tuple


class GateOutcome(str, Enum):
    ARMED = "armed"
    BLOCKED_DONCHIAN = "blocked_donchian"
    BLOCKED_MACRO = "blocked_macro"
    BLOCKED_MOMENTUM = "blocked_momentum"
    INSUFFICIENT_HISTORY = "insufficient_history"


@dataclass(frozen=True)
class EntryGateConfig:
    """Configuration for the 3-layer entry gate.

    All toggles can be disabled by setting their threshold to a no-op value:
      - donchian_lookback <= 0 disables Layer 1.
      - macro_ma_lookback <= 0 disables Layer 2.
      - require_momentum_uptick=False disables Layer 3.
    """

    # Layer 1: Donchian low trigger.
    donchian_lookback: int = 20
    donchian_tolerance_pct: float = 0.5   # current price <= min(low,N) * (1 + tol/100)

    # Layer 2: Macro MA filter on higher TF (kept agnostic to the actual TF;
    # caller passes the higher-TF candle history).
    macro_ma_lookback: int = 20
    macro_ma_source: str = "close"        # "close" or "low"
    macro_drop_pct: float = 30.0          # allow price up to 30% below the MA

    # Layer 3: Momentum confirmation.
    require_momentum_uptick: bool = True
    momentum_source: str = "close"        # "close" or "ha_close"

    # Future extensions documented but not used yet:
    require_volume_uptick: bool = False
    volume_ratio_min: float = 1.5


@dataclass(frozen=True)
class GateDecision:
    outcome: GateOutcome
    armed: bool
    reason: str
    donchian_low: Optional[float] = None
    donchian_trigger_price: Optional[float] = None
    macro_ma: Optional[float] = None
    macro_floor: Optional[float] = None
    last_close: Optional[float] = None
    prev_close: Optional[float] = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "armed": self.armed,
            "reason": self.reason,
            "donchian_low": self.donchian_low,
            "donchian_trigger_price": self.donchian_trigger_price,
            "macro_ma": self.macro_ma,
            "macro_floor": self.macro_floor,
            "last_close": self.last_close,
            "prev_close": self.prev_close,
            **self.metadata,
        }


def _candle_field(candle: dict, key: str, default: float = 0.0) -> float:
    v = candle.get(key, default)
    try:
        return float(v)
    except Exception:
        return float(default)


def evaluate_entry_gate(
    *,
    operating_history: Sequence[dict],
    macro_history: Sequence[dict],
    current_price: float,
    config: EntryGateConfig = EntryGateConfig(),
) -> GateDecision:
    """Decide if NOW is a good arming moment.

    Parameters
    ----------
    operating_history:
        Last N candles in the operating timeframe (e.g. 15m). Each candle
        is a dict with at least 'low' and 'close'. Index -1 is the most
        recent CLOSED candle (the current forming candle is NOT included).
    macro_history:
        Last M candles in the higher timeframe (e.g. 4h). Same shape.
    current_price:
        Latest tick price (typically the close of the current forming
        operating-TF candle or the WS trade price).
    config:
        EntryGateConfig.
    """
    op_n = len(operating_history)
    macro_n = len(macro_history)

    required_op = max(config.donchian_lookback, 2)
    required_macro = max(config.macro_ma_lookback, 2)
    if op_n < required_op or macro_n < required_macro:
        return GateDecision(
            outcome=GateOutcome.INSUFFICIENT_HISTORY,
            armed=False,
            reason=f"need op>={required_op} (have {op_n}); macro>={required_macro} (have {macro_n})",
        )

    last_close = _candle_field(operating_history[-1], "close")
    prev_close = _candle_field(operating_history[-2], "close") if op_n >= 2 else last_close

    # --- Layer 1: Donchian low(N) trigger ---
    donchian_low = None
    donchian_trigger = None
    if config.donchian_lookback > 0:
        window = operating_history[-config.donchian_lookback:]
        donchian_low = min(_candle_field(c, "low") for c in window)
        donchian_trigger = donchian_low * (1.0 + config.donchian_tolerance_pct / 100.0)
        if current_price > donchian_trigger:
            return GateDecision(
                outcome=GateOutcome.BLOCKED_DONCHIAN,
                armed=False,
                reason=f"price {current_price:.6f} > donchian trigger {donchian_trigger:.6f}",
                donchian_low=donchian_low,
                donchian_trigger_price=donchian_trigger,
                last_close=last_close,
                prev_close=prev_close,
            )

    # --- Layer 2: Macro MA floor ---
    macro_ma = None
    macro_floor = None
    if config.macro_ma_lookback > 0:
        window = macro_history[-config.macro_ma_lookback:]
        values = [_candle_field(c, config.macro_ma_source) for c in window]
        macro_ma = sum(values) / len(values)
        macro_floor = macro_ma * (1.0 - config.macro_drop_pct / 100.0)
        if current_price < macro_floor:
            return GateDecision(
                outcome=GateOutcome.BLOCKED_MACRO,
                armed=False,
                reason=(
                    f"price {current_price:.6f} < macro floor {macro_floor:.6f} "
                    f"(MA={macro_ma:.6f} - {config.macro_drop_pct}%)"
                ),
                donchian_low=donchian_low,
                donchian_trigger_price=donchian_trigger,
                macro_ma=macro_ma,
                macro_floor=macro_floor,
                last_close=last_close,
                prev_close=prev_close,
            )

    # --- Layer 3: Momentum uptick ---
    if config.require_momentum_uptick:
        if config.momentum_source == "ha_close":
            ha_close = _candle_field(operating_history[-1], "ha_close", last_close)
            ha_open = _candle_field(operating_history[-1], "ha_open", last_close)
            uptick = ha_close > ha_open
            metric = {"ha_close": ha_close, "ha_open": ha_open}
        else:
            uptick = last_close > prev_close
            metric = {"last_close": last_close, "prev_close": prev_close}
        if not uptick:
            return GateDecision(
                outcome=GateOutcome.BLOCKED_MOMENTUM,
                armed=False,
                reason=f"no uptick {metric}",
                donchian_low=donchian_low,
                donchian_trigger_price=donchian_trigger,
                macro_ma=macro_ma,
                macro_floor=macro_floor,
                last_close=last_close,
                prev_close=prev_close,
            )

    return GateDecision(
        outcome=GateOutcome.ARMED,
        armed=True,
        reason="all layers passed",
        donchian_low=donchian_low,
        donchian_trigger_price=donchian_trigger,
        macro_ma=macro_ma,
        macro_floor=macro_floor,
        last_close=last_close,
        prev_close=prev_close,
    )


# --- Live WS monitor stub (interface only, no network) ---

class AgarthaWsMonitor:
    """Minimal interface for a live WS-driven entry monitor.

    The actual connector (live) wires this to:
      - Binance Alpha WS @kline_15m for operating history.
      - Binance Alpha WS @kline_4h for macro history.
      - Binance Alpha WS @trade or @miniTicker for current_price ticks.

    The monitor maintains rolling deques per timeframe and on every tick
    calls `evaluate_entry_gate(...)`. When ARMED, emits a `READY_TO_ARM`
    event to the operator (or auto-launches the bot if `auto_arm=True`).

    This stub keeps the rolling state in memory; the live connector
    subclasses or composes this with the WS client.
    """

    def __init__(
        self,
        config: EntryGateConfig = EntryGateConfig(),
        *,
        op_capacity: int = 200,
        macro_capacity: int = 200,
    ):
        from collections import deque
        self.config = config
        self.op_history = deque(maxlen=int(op_capacity))
        self.macro_history = deque(maxlen=int(macro_capacity))
        self.last_decision: Optional[GateDecision] = None

    def push_operating_candle(self, candle: dict) -> None:
        self.op_history.append(candle)

    def push_macro_candle(self, candle: dict) -> None:
        self.macro_history.append(candle)

    def on_tick(self, current_price: float) -> GateDecision:
        decision = evaluate_entry_gate(
            operating_history=list(self.op_history),
            macro_history=list(self.macro_history),
            current_price=float(current_price),
            config=self.config,
        )
        self.last_decision = decision
        return decision
