"""Deterministic idempotency keys for backtest runs.

A run's identity is the hash of its inputs, normalised so that semantically
equivalent configurations always produce the same key (regardless of dict
ordering or trailing float precision).
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict


_FLOAT_DECIMALS = 12


def _canonicalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _canonicalize(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    if isinstance(value, bool):
        # bool is a subclass of int, keep it ahead of int.
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        # Round to fixed decimals and re-parse to drop trailing zeros consistently.
        rounded = round(value, _FLOAT_DECIMALS)
        # `-0.0 == 0.0` but their JSON encoding differs; force a positive zero.
        if rounded == 0.0:
            rounded = 0.0
        return rounded
    return value


def canonical_params_json(params: Dict[str, Any]) -> str:
    """Return a canonical JSON encoding of `params`.

    Keys are recursively sorted; floats are rounded to 12 decimals; bools and
    None are preserved. The result is a deterministic string suitable for
    hashing.
    """
    if not isinstance(params, dict):
        raise TypeError(f"params must be a dict, got {type(params).__name__}")
    canonical = _canonicalize(params)
    return json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_run_key(
    *,
    strategy: str,
    symbol: str,
    interval: str,
    start_ts: int | None,
    end_ts: int | None,
    initial_cash: float,
    fee_rate: float,
    slippage_bps: float,
    strategy_params: Dict[str, Any],
    engine_kind: str,
    engine_version: str,
) -> str:
    """Return the sha256 hex digest that uniquely identifies a run."""
    payload = {
        "strategy": str(strategy),
        "symbol": str(symbol),
        "interval": str(interval),
        "start_ts": int(start_ts) if start_ts is not None else None,
        "end_ts": int(end_ts) if end_ts is not None else None,
        "initial_cash": float(initial_cash),
        "fee_rate": float(fee_rate),
        "slippage_bps": float(slippage_bps),
        "strategy_params": strategy_params or {},
        "engine_kind": str(engine_kind),
        "engine_version": str(engine_version),
    }
    canonical = canonical_params_json(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = ["canonical_params_json", "compute_run_key"]
