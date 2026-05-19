"""Built-in example strategies."""
from backtest.strategy_base import Signal, StrategyBase, StrategyContext


class SmaCrossStrategy(StrategyBase):
    name = "sma_cross"

    def __init__(self, fast: int = 10, slow: int = 30, **params):
        super().__init__(fast=fast, slow=slow, **params)
        self.fast = int(fast)
        self.slow = int(slow)

    def on_bar(self, ctx: StrategyContext) -> Signal:
        if ctx.index < max(self.fast, self.slow):
            return Signal(action="hold", reason="warmup")
        c = ctx.candles[ctx.index]
        prev = ctx.candles[ctx.index - 1]
        c_fast = c.get(f"sma_{self.fast}", c.get("sma"))
        c_slow = c.get(f"sma_{self.slow}", c.get("ema"))
        p_fast = prev.get(f"sma_{self.fast}", prev.get("sma"))
        p_slow = prev.get(f"sma_{self.slow}", prev.get("ema"))
        if None in (c_fast, c_slow, p_fast, p_slow):
            return Signal(action="hold", reason="indicator_none")
        if p_fast <= p_slow and c_fast > c_slow:
            return Signal(action="buy", reason="golden_cross")
        if p_fast >= p_slow and c_fast < c_slow and ctx.position_qty > 0:
            return Signal(action="sell", reason="death_cross")
        return Signal(action="hold")

