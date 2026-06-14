"""Dorothy sizing accessories (VolumenCompuesto, helpers).

VolumenIncremental remains a separate accessory; do not combine both on the same run.
Monetary math uses :class:`decimal.Decimal` to avoid float drift on USDT lot sizing.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Tuple, Union

Number = Union[Decimal, int, float, str]


def _to_decimal(value: Number) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def decimal_to_str(value: Decimal) -> str:
    """Canonical string for audit metadata (no float, trim trailing zeros)."""
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def volumen_compuesto_factor(
    equity: Number,
    initial_equity: Number,
    *,
    greed_factor: Number = "0",
) -> Decimal:
    """Return ``(equity / initial_equity) * (1 + greed_factor)``.

    With ``greed_factor=0`` this is plain equity ratio (1.0 at par, 1.1 at +10 %).
    A light ``greed_factor`` (e.g. 0.01) adds a small adaptive boost that scales
    with equity performance: at 1100 vs 1000 initial, factor = 1.1 * 1.01 = 1.111.
    """
    base = _to_decimal(initial_equity)
    if base <= 0:
        return Decimal("1")
    eq = _to_decimal(equity)
    ratio = eq / base
    greed = _to_decimal(greed_factor)
    if greed < 0:
        greed = Decimal("0")
    factor = ratio * (Decimal("1") + greed)
    return max(Decimal("0"), factor)


def volumen_compuesto_notional(
    *,
    base_quote_usdt: Number,
    equity: Number,
    initial_equity: Number,
    min_quote_usdt: Number = "6",
    greed_factor: Number = "0",
) -> Tuple[Decimal, Decimal]:
    """Compute buy notional as ``base * VC_factor`` with a USDT floor.

    ``VC_factor = (equity/initial) * (1 + greed_factor)``.

    Examples (base=8, initial=1000, min=6, greed=0):
      - equity 1000 -> factor 1.0 -> 8.0 USDT
      - equity 1100 -> factor 1.1 -> 8.8 USDT
      - equity  900 -> factor 0.9 -> 7.2 USDT
      - equity  500 -> factor 0.5 -> 6.0 USDT (floor)

    With greed=0.01 and equity 1100 -> factor 1.111 -> 8.888 USDT.
    """
    base = _to_decimal(base_quote_usdt)
    floor = _to_decimal(min_quote_usdt)
    factor = volumen_compuesto_factor(
        equity,
        initial_equity,
        greed_factor=greed_factor,
    )
    raw = base * factor
    notional = max(floor, raw)
    return notional, factor


__all__ = [
    "_to_decimal",
    "decimal_to_str",
    "volumen_compuesto_factor",
    "volumen_compuesto_notional",
]
