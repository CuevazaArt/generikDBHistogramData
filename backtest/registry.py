"""Strategy registry and parameter/search-space helpers."""
from typing import Any, Dict, Type

from backtest.strategy_base import StrategyBase
from backtest.strategies import DorothyBacktestStrategy, SmaCrossStrategy


STRATEGY_REGISTRY: Dict[str, Type[StrategyBase]] = {
    SmaCrossStrategy.name: SmaCrossStrategy,
    DorothyBacktestStrategy.name: DorothyBacktestStrategy,
}


def get_strategy(strategy_name: str) -> Type[StrategyBase]:
    key = (strategy_name or "").strip().lower()
    if key not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy '{strategy_name}'. Available: {', '.join(sorted(STRATEGY_REGISTRY))}")
    return STRATEGY_REGISTRY[key]


def params_from_cli(args: Any, strategy_name: str) -> Dict[str, Any]:
    key = strategy_name.strip().lower()
    if key == "dorothy":
        return {
            "profit_factor": float(args.profit_factor),
            "margin_drop_factor": float(args.margin_drop_factor),
            "quote_order_qty_usdt": float(args.quote_order_qty_usdt),
            "min_order_notional": float(args.min_order_notional),
            "max_order_notional": float(args.max_order_notional),
            "max_active_orders": int(args.max_active_orders),
        }
    return {
        "fast": int(args.fast),
        "slow": int(args.slow),
    }


def suggest_params(trial: Any, strategy_name: str) -> Dict[str, Any]:
    key = strategy_name.strip().lower()
    if key == "dorothy":
        return {
            "profit_factor": float(trial.suggest_float("profit_factor", 0.005, 0.08)),
            "margin_drop_factor": float(trial.suggest_float("margin_drop_factor", 0.001, 0.02)),
            "quote_order_qty_usdt": 8.0,
            "min_order_notional": 6.0,
            "max_order_notional": 10.0,
            "max_active_orders": 200,
        }
    params = {
        "fast": int(trial.suggest_int("fast", 5, 40)),
        "slow": int(trial.suggest_int("slow", 20, 120)),
    }
    if params["fast"] >= params["slow"]:
        # Caller can prune invalid combinations.
        params["_invalid"] = True
    return params

