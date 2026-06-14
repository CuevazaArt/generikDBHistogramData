"""Type stubs for the compiled ``genericbt_core._genericbt_core`` extension.

The real implementation is the maturin-built ``_genericbt_core.{abi3.so,pyd}``
that lands next to this file. We ship the stub so that static analysers
(mypy, pyright) see the public surface even when the wheel is not built
locally — without the stub, ``from . import _genericbt_core as _rust``
would type-check as ``Any`` and every call site would lose hints.
"""

from typing import Any, Dict, List

__version__: str

def run_backtest(
    config: Dict[str, Any],
    strategy: Any,
    candles: List[Dict[str, Any]],
    run_id: int | None = ...,
    trial_id: int | None = ...,
) -> Dict[str, Any]:
    """Native bar loop. Returns dict with keys
    ``metrics`` / ``events`` / ``equity_curve`` / ``final_state`` /
    ``run_id`` / ``trial_id``.

    The ``config`` dict accepts (in addition to the Fase 1 keys) the
    Fase 2 checkpointing knobs: ``checkpoint_every_bars`` (int | None),
    ``checkpoint_every_sim_seconds`` (int | None), ``checkpoints_dir``
    (str | None) and ``resume_from_checkpoint`` (str | None). Unknown
    keys are ignored by the Rust side.
    """
    ...

def sma(values: List[float], period: int) -> List[float | None]: ...
def apply_indicators(
    candles: List[Dict[str, Any]],
    sma_period: int,
    ema_period: int,
    rsi_period: int,
    atr_period: int,
    price_key: str,
) -> None: ...
def apply_heikin_ashi(candles: List[Dict[str, Any]]) -> List[Dict[str, Any]]: ...
def apply_candle_source(candles: List[Dict[str, Any]], source: str) -> None: ...

class SpotBrokerRs:
    cash: float
    position_qty: float
    avg_entry: float
    fee_rate: float
    slippage_bps: float

    def __init__(
        self,
        initial_cash: float,
        fee_rate: float = ...,
        slippage_bps: float = ...,
    ) -> None: ...
    def mark_equity(self, mark_price: float) -> float: ...
    def execute_market(
        self,
        side: str,
        price: float,
        size_pct: float = ...,
    ) -> Dict[str, Any] | None: ...
