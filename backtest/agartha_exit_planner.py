"""Agartha exit planner: trailing-stop + fallback when LIMIT cannot be placed.

This module is engine-agnostic and side-effect-free: dado el estado de la
posicion y los filtros del exchange (PERCENT_PRICE_BY_SIDE, tickSize, etc.),
calcula:

  1. El **trail_floor** (precio a partir del cual debe disparar el trailing).
  2. El **precio LIMIT a colocar** respetando filtros del exchange.
  3. Si la LIMIT esta fuera de banda (Binance Alpha rechaza PERCENT_PRICE_BY_SIDE),
     evalua una **logica alternativa**:
       - Detectar una **cresta confirmada** (peak previo + retroceso suficiente).
       - Re-cotizar al **borde inferior** de la banda permitida.
       - Marcar la decision con el motivo del fallback para auditoria.

Se usa tanto en backtest (para simular la decision con realismo) como en live
(donde el bot la consume tick-a-tick antes de mandar la orden REST).

Decision tecnica registrada en library/bots/agartha/notes.md:
  "El trailing stop es 100% responsabilidad del bot. Binance Alpha solo
   acepta LIMIT; no hay TRAILING_STOP_MARKET ni STOP_LOSS server-side."
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence


class ExitAction(str, Enum):
    HOLD = "hold"
    TRAIL_LIMIT = "trail_limit"       # LIMIT dentro de banda
    TRAIL_BORDER = "trail_border"     # LIMIT al borde inferior de banda (fallback)
    CREST_CONFIRMED = "crest_confirmed"  # vender por cresta detectada
    OUT_OF_BAND = "out_of_band"       # no se puede colocar LIMIT, hold con alerta


@dataclass(frozen=True)
class SymbolFilters:
    """Subset de filtros del exchange relevantes para el exit planner."""
    tick_size: float = 0.00000001
    bid_multiplier_down: float = 0.2   # PERCENT_PRICE_BY_SIDE bid lower bound
    bid_multiplier_up: float = 5.0     # PERCENT_PRICE_BY_SIDE bid upper bound
    ask_multiplier_down: float = 0.2
    ask_multiplier_up: float = 5.0
    min_notional: float = 0.1


@dataclass(frozen=True)
class ExitPlan:
    """Resultado del planner: que hacer este tick."""
    action: ExitAction
    limit_price: Optional[float] = None
    reason: str = ""
    peak_price: float = 0.0
    trail_floor: float = 0.0
    band_lower: float = 0.0
    band_upper: float = 0.0
    crest_detected: bool = False
    fallback_used: bool = False
    metadata: dict = field(default_factory=dict)


def floor_to_tick(price: float, tick_size: float) -> float:
    """Redondea hacia abajo al multiplo de tick_size (conservador para SELL LIMIT)."""
    if tick_size <= 0 or price <= 0:
        return float(price)
    return (int(price / tick_size + 1e-12)) * tick_size


def ceil_to_tick(price: float, tick_size: float) -> float:
    """Redondea hacia arriba al multiplo de tick_size."""
    if tick_size <= 0 or price <= 0:
        return float(price)
    import math
    return math.ceil(price / tick_size - 1e-12) * tick_size


def compute_trail_floor(
    *,
    peak_price: float,
    entry_price: float,
    trailing_stop_pct: float,
    breakeven_lock_pct: float = 0.0,
) -> float:
    """Calcula el floor a partir del cual el trailing debe disparar.

    Si breakeven_lock_pct > 0 y peak >= entry*(1+breakeven_lock_pct/100),
    el floor nunca baja del entry_price.
    """
    if peak_price <= 0 or trailing_stop_pct <= 0:
        return 0.0
    floor = peak_price * (1.0 - trailing_stop_pct / 100.0)
    if breakeven_lock_pct > 0 and entry_price > 0:
        breakeven_at = entry_price * (1.0 + breakeven_lock_pct / 100.0)
        if peak_price >= breakeven_at:
            floor = max(floor, entry_price)
    return float(floor)


def compute_sell_band(price_reference: float, filters: SymbolFilters) -> tuple[float, float]:
    """Calcula la banda permitida para una SELL LIMIT segun PERCENT_PRICE_BY_SIDE.

    En Binance Alpha, una SELL LIMIT necesita estar entre
    [price * ask_multiplier_down, price * ask_multiplier_up]. price_reference
    suele ser el ultimo precio del libro o close.
    """
    if price_reference <= 0:
        return 0.0, 0.0
    lower = price_reference * float(filters.ask_multiplier_down)
    upper = price_reference * float(filters.ask_multiplier_up)
    return float(lower), float(upper)


def detect_crest(
    recent_highs: Sequence[float],
    current_price: float,
    min_pullback_pct: float = 15.0,
) -> bool:
    """Heuristica de cresta confirmada.

    Una cresta se considera confirmada si el max de las ultimas N velas supera
    el precio actual por al menos `min_pullback_pct`. Pensada para detectar
    pumps que ya se estan agotando incluso si el trailing aun no disparo.
    """
    if not recent_highs or current_price <= 0:
        return False
    peak = max(recent_highs)
    if peak <= 0:
        return False
    pullback = (peak - current_price) / peak * 100.0
    return pullback >= float(min_pullback_pct)


def plan_exit(
    *,
    current_price: float,
    entry_price: float,
    peak_price: float,
    trailing_stop_pct: float,
    breakeven_lock_pct: float = 0.0,
    filters: SymbolFilters = SymbolFilters(),
    recent_highs: Optional[Sequence[float]] = None,
    crest_pullback_pct: float = 15.0,
    aggressive_limit_offset_ticks: int = 1,
) -> ExitPlan:
    """Decide la accion de salida para el tick actual.

    Reglas (en orden):
      1. Si peak/trailing no son utiles -> HOLD.
      2. Calcula trail_floor. Si current_price > trail_floor -> HOLD.
      3. Trailing disparo: queremos vender. Calcula precio LIMIT objetivo
         (current_price ajustado a tick).
      4. Verifica banda PERCENT_PRICE_BY_SIDE.
           - Si dentro: action = TRAIL_LIMIT.
           - Si abajo de la banda: re-cotiza al borde inferior (TRAIL_BORDER).
           - Si arriba de la banda: imposible para SELL -> OUT_OF_BAND.
      5. Detector de cresta (opcional, si recent_highs disponible): si hubo
         pullback grande desde un peak local, marca crest_detected aunque el
         trailing principal no haya disparado todavia.
    """
    if current_price <= 0 or entry_price <= 0 or peak_price <= 0:
        return ExitPlan(action=ExitAction.HOLD, reason="invalid_state")

    trail_floor = compute_trail_floor(
        peak_price=peak_price,
        entry_price=entry_price,
        trailing_stop_pct=trailing_stop_pct,
        breakeven_lock_pct=breakeven_lock_pct,
    )

    # Cresta confirmada (independiente del trailing).
    crest = False
    if recent_highs:
        crest = detect_crest(
            recent_highs=recent_highs,
            current_price=current_price,
            min_pullback_pct=crest_pullback_pct,
        )

    triggered = trail_floor > 0 and current_price <= trail_floor
    if not triggered and not crest:
        return ExitPlan(
            action=ExitAction.HOLD,
            reason="trail_not_triggered",
            peak_price=peak_price,
            trail_floor=trail_floor,
        )

    # Calcular banda permitida y precio LIMIT objetivo.
    band_lower, band_upper = compute_sell_band(current_price, filters)
    # Para SELL agresiva queremos colocar la LIMIT cerca del precio actual o
    # ligeramente por debajo para asegurar fill rapido.
    target = current_price - float(aggressive_limit_offset_ticks) * filters.tick_size
    target = floor_to_tick(target, filters.tick_size)

    if target <= 0:
        target = floor_to_tick(current_price, filters.tick_size)

    if band_lower <= target <= band_upper:
        return ExitPlan(
            action=ExitAction.TRAIL_LIMIT if triggered else ExitAction.CREST_CONFIRMED,
            limit_price=target,
            reason="trail_triggered" if triggered else "crest_detected",
            peak_price=peak_price,
            trail_floor=trail_floor,
            band_lower=band_lower,
            band_upper=band_upper,
            crest_detected=crest,
            fallback_used=False,
        )

    # Fuera de banda. Si esta debajo, podemos re-cotizar al borde inferior.
    if target < band_lower:
        border_price = ceil_to_tick(band_lower, filters.tick_size)
        return ExitPlan(
            action=ExitAction.TRAIL_BORDER,
            limit_price=border_price,
            reason="limit_below_band_recoted_to_lower_bound",
            peak_price=peak_price,
            trail_floor=trail_floor,
            band_lower=band_lower,
            band_upper=band_upper,
            crest_detected=crest,
            fallback_used=True,
        )

    # Si esta arriba (poco probable para SELL): no se puede colocar.
    return ExitPlan(
        action=ExitAction.OUT_OF_BAND,
        limit_price=None,
        reason="limit_above_band_unplaceable",
        peak_price=peak_price,
        trail_floor=trail_floor,
        band_lower=band_lower,
        band_upper=band_upper,
        crest_detected=crest,
        fallback_used=True,
    )
