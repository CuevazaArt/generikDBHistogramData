"""Shared Decimal helpers for all bot runners.

Eliminates the _dec / _q duplication across dorothy.py and elphaba.py.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any


def dec(x: Any, default: str = "0") -> Decimal:
    """Safe Decimal conversion with fallback."""
    try:
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def quantize(x: Decimal, places: int) -> Decimal:
    """Truncate (floor) a Decimal to *places* decimal digits."""
    if places < 0:
        places = 0
    step = Decimal(10) ** Decimal(-places)
    return x.quantize(step, rounding=ROUND_DOWN)
