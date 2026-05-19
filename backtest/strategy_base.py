"""Strategy API for pluggable bot logic."""
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Signal:
    action: str  # buy, sell, hold
    size_pct: float = 1.0
    reason: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class StrategyContext:
    index: int
    candle: Dict[str, Any]
    candles: List[Dict[str, Any]]
    cash: float
    position_qty: float
    avg_entry: float
    equity: float


class StrategyBase:
    name = "base"

    def __init__(self, **params: Any):
        self.params = params

    def on_start(self, candles: List[Dict[str, Any]]) -> None:
        _ = candles

    def on_bar(self, ctx: StrategyContext) -> Signal:
        _ = ctx
        return Signal(action="hold")

    def on_fill(self, fill: Dict[str, Any], signal: Signal, ctx: StrategyContext) -> None:
        _ = (fill, signal, ctx)

    def on_finish(self) -> None:
        return None

