"""Strategy registry and parameter/search-space helpers."""
from typing import Any, Dict, Optional, Type

from backtest.strategy_base import StrategyBase
from backtest.strategies import (
    DorothyBacktestStrategy,
    DorothyHubStrategy,
    ElphabaHubStrategy,
    HeikinAshiTrendStrategy,
    MashaPlaceholderStrategy,
    SmaCrossStrategy,
    ThusneldaPlaceholderStrategy,
)


STRATEGY_REGISTRY: Dict[str, Type[StrategyBase]] = {
    SmaCrossStrategy.name: SmaCrossStrategy,
    DorothyBacktestStrategy.name: DorothyBacktestStrategy,
    DorothyHubStrategy.name: DorothyHubStrategy,
    ElphabaHubStrategy.name: ElphabaHubStrategy,
    HeikinAshiTrendStrategy.name: HeikinAshiTrendStrategy,
    MashaPlaceholderStrategy.name: MashaPlaceholderStrategy,
    ThusneldaPlaceholderStrategy.name: ThusneldaPlaceholderStrategy,
    # Placeholders for newly imported Louise family.
    "louise": MashaPlaceholderStrategy,
    "anti_louise": ThusneldaPlaceholderStrategy,
    "louise_lucky": MashaPlaceholderStrategy,
    "anti_louise_lucky": ThusneldaPlaceholderStrategy,
    # Backward-compatible aliases from previous naming.
    "dorothy_hub": DorothyHubStrategy,
    "elphaba_hub": ElphabaHubStrategy,
}

ALIAS_STRATEGY_NAMES = {"dorothy_hub", "elphaba_hub"}


def get_strategy(strategy_name: str) -> Type[StrategyBase]:
    key = (strategy_name or "").strip().lower()
    if key not in STRATEGY_REGISTRY:
        raise ValueError(f"Unknown strategy '{strategy_name}'. Available: {', '.join(sorted(STRATEGY_REGISTRY))}")
    return STRATEGY_REGISTRY[key]


def list_strategy_names(include_aliases: bool = False) -> list[str]:
    names = sorted(STRATEGY_REGISTRY.keys())
    if include_aliases:
        return names
    return [n for n in names if n not in ALIAS_STRATEGY_NAMES]


def params_from_cli(args: Any, strategy_name: str) -> Dict[str, Any]:
    key = strategy_name.strip().lower()
    if key in ("dorothy", "dorothy_hub", "dorothy_legacy"):
        return {
            "profit_factor": float(args.profit_factor),
            "margin_drop_factor": float(args.margin_drop_factor),
            "quote_order_qty_usdt": float(args.quote_order_qty_usdt),
            "min_order_notional": float(args.min_order_notional),
            "max_order_notional": float(args.max_order_notional),
            "max_active_orders": int(args.max_active_orders) if hasattr(args, "max_active_orders") else 200,
            "max_rungs": int(args.max_rungs) if hasattr(args, "max_rungs") else 5,
        }
    if key in ("elphaba", "elphaba_hub"):
        return {
            "profit_factor": float(args.profit_factor),
            "margin_rise_factor": float(args.margin_rise_factor),
            "quote_order_qty_usdt": float(args.quote_order_qty_usdt),
            "max_rungs": int(args.max_rungs),
        }
    if key == "ha_trend":
        return {
            "trend_mode": str(args.trend_mode),
            "quote_order_qty_usdt": float(args.quote_order_qty_usdt),
        }
    if key in ("masha", "thusnelda", "louise", "anti_louise", "louise_lucky", "anti_louise_lucky"):
        return {
            "placeholder_level": int(getattr(args, "placeholder_level", 1)),
        }
    return {
        "fast": int(args.fast),
        "slow": int(args.slow),
    }


def _get_float_range(
    overrides: Dict[str, Any],
    min_key: str,
    max_key: str,
    default_min: float,
    default_max: float,
) -> tuple[float, float]:
    lo = float(overrides.get(min_key, default_min))
    hi = float(overrides.get(max_key, default_max))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def _get_int_range(
    overrides: Dict[str, Any],
    min_key: str,
    max_key: str,
    default_min: int,
    default_max: int,
) -> tuple[int, int]:
    lo = int(overrides.get(min_key, default_min))
    hi = int(overrides.get(max_key, default_max))
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def suggest_params(trial: Any, strategy_name: str, search_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    key = strategy_name.strip().lower()
    overrides = search_overrides or {}
    if key in ("dorothy", "dorothy_hub", "dorothy_legacy"):
        pf_min, pf_max = _get_float_range(overrides, "profit_factor_min", "profit_factor_max", 0.005, 0.08)
        md_min, md_max = _get_float_range(overrides, "margin_drop_factor_min", "margin_drop_factor_max", 0.001, 0.02)
        r_min, r_max = _get_int_range(overrides, "max_rungs_min", "max_rungs_max", 2, 10)
        return {
            "profit_factor": float(trial.suggest_float("profit_factor", pf_min, pf_max)),
            "margin_drop_factor": float(trial.suggest_float("margin_drop_factor", md_min, md_max)),
            "quote_order_qty_usdt": 8.0,
            "min_order_notional": 6.0,
            "max_order_notional": 10.0,
            "max_active_orders": 200,
            "max_rungs": int(trial.suggest_int("max_rungs", r_min, r_max)),
        }
    if key in ("elphaba", "elphaba_hub"):
        pf_min, pf_max = _get_float_range(overrides, "profit_factor_min", "profit_factor_max", 0.005, 0.08)
        mr_min, mr_max = _get_float_range(overrides, "margin_rise_factor_min", "margin_rise_factor_max", 0.005, 0.05)
        r_min, r_max = _get_int_range(overrides, "max_rungs_min", "max_rungs_max", 2, 10)
        return {
            "profit_factor": float(trial.suggest_float("profit_factor", pf_min, pf_max)),
            "margin_rise_factor": float(trial.suggest_float("margin_rise_factor", mr_min, mr_max)),
            "quote_order_qty_usdt": 8.0,
            "max_rungs": int(trial.suggest_int("max_rungs", r_min, r_max)),
        }
    if key == "ha_trend":
        return {
            "trend_mode": trial.suggest_categorical("trend_mode", ["both", "long", "short"]),
            "quote_order_qty_usdt": 8.0,
        }
    if key in ("masha", "thusnelda", "louise", "anti_louise", "louise_lucky", "anti_louise_lucky"):
        # Placeholder: keep a no-op parameter to allow Optuna flow/tests.
        return {"placeholder_level": int(trial.suggest_int("placeholder_level", 1, 3))}
    fast_min, fast_max = _get_int_range(overrides, "fast_min", "fast_max", 5, 40)
    slow_min, slow_max = _get_int_range(overrides, "slow_min", "slow_max", 20, 120)
    params = {
        "fast": int(trial.suggest_int("fast", fast_min, fast_max)),
        "slow": int(trial.suggest_int("slow", slow_min, slow_max)),
    }
    if params["fast"] >= params["slow"]:
        # Caller can prune invalid combinations.
        params["_invalid"] = True
    return params

