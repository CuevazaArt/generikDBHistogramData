"""Strategy registry and parameter/search-space helpers."""
from decimal import Decimal, ROUND_FLOOR
from typing import Any, Callable, Dict, Optional, Type

from backtest.strategy_base import StrategyBase
from backtest.strategies import (
    AntiLouiseLuckyStrategy,
    AntiLouiseStrategy,
    DorothyBacktestStrategy,
    DorothyHubStrategy,
    ElphabaHubStrategy,
    HeikinAshiTrendStrategy,
    LouiseLuckyStrategy,
    LouiseStrategy,
    MashaStrategy,
    SmaCrossStrategy,
    ThusneldaPlaceholderStrategy,
)


STRATEGY_REGISTRY: Dict[str, Type[StrategyBase]] = {
    SmaCrossStrategy.name: SmaCrossStrategy,
    DorothyBacktestStrategy.name: DorothyBacktestStrategy,
    DorothyHubStrategy.name: DorothyHubStrategy,
    ElphabaHubStrategy.name: ElphabaHubStrategy,
    HeikinAshiTrendStrategy.name: HeikinAshiTrendStrategy,
    MashaStrategy.name: MashaStrategy,
    ThusneldaPlaceholderStrategy.name: ThusneldaPlaceholderStrategy,
    LouiseStrategy.name: LouiseStrategy,
    AntiLouiseStrategy.name: AntiLouiseStrategy,
    LouiseLuckyStrategy.name: LouiseLuckyStrategy,
    AntiLouiseLuckyStrategy.name: AntiLouiseLuckyStrategy,
    # Backward-compatible aliases from previous naming.
    "dorothy_hub": DorothyHubStrategy,
    "elphaba_hub": ElphabaHubStrategy,
}

ALIAS_STRATEGY_NAMES = {"dorothy_hub", "elphaba_hub"}


# Override hooks populated by :func:`backtest.library.register_with_strategy_registry`.
# Library entries that introduce brand-new strategy names register a callable here
# so the legacy hard-coded if/elif chain in :func:`params_from_cli` /
# :func:`suggest_params` is bypassed for them. Existing strategies keep their
# explicit cases intact so we maintain 100% backward compatibility.
PARAMS_FROM_CLI_OVERRIDES: Dict[str, Callable[[Any], Dict[str, Any]]] = {}
SUGGEST_PARAMS_OVERRIDES: Dict[str, Callable[..., Dict[str, Any]]] = {}


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
    override = PARAMS_FROM_CLI_OVERRIDES.get(key)
    if override is not None and key not in (
        "dorothy",
        "dorothy_hub",
        "dorothy_legacy",
        "elphaba",
        "elphaba_hub",
        "ha_trend",
        "masha",
        "louise",
        "louise_lucky",
        "anti_louise",
        "anti_louise_lucky",
        "thusnelda",
        "sma_cross",
    ):
        return override(args)
    if key in ("dorothy", "dorothy_hub"):
        return {
            "profit_factor": float(args.profit_factor),
            "margin_drop_factor": float(args.margin_drop_factor),
            "quote_order_qty_usdt": float(args.quote_order_qty_usdt),
            "max_rungs": int(args.max_rungs) if hasattr(args, "max_rungs") else 5,
        }
    if key == "dorothy_legacy":
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
    if key == "masha":
        return {
            "fast": int(getattr(args, "fast", 9)),
            "slow": int(getattr(args, "slow", 34)),
            "quote_order_qty_usdt": float(getattr(args, "quote_order_qty_usdt", 8.0)),
            "take_profit_pct": float(getattr(args, "take_profit_pct", 1.5)),
            "stop_loss_pct": float(getattr(args, "stop_loss_pct", 4.0)),
            "pullback_factor": float(getattr(args, "pullback_factor", 0.006)),
        }
    if key in ("louise", "louise_lucky"):
        params = {
            "target_profit_pct": float(getattr(args, "target_profit_pct", 1.5)),
            "margin_drop_factor": float(getattr(args, "margin_drop_factor", 0.004)),
            "quote_order_qty_usdt": float(getattr(args, "quote_order_qty_usdt", 8.0)),
        }
        if key.endswith("_lucky"):
            params["lucky_window"] = int(getattr(args, "lucky_window", 24))
        return params
    if key in ("anti_louise", "anti_louise_lucky"):
        params = {
            "target_profit_pct": float(getattr(args, "target_profit_pct", 1.5)),
            "margin_rise_factor": float(getattr(args, "margin_rise_factor", 0.004)),
            "quote_order_qty_usdt": float(getattr(args, "quote_order_qty_usdt", 8.0)),
        }
        if key.endswith("_lucky"):
            params["lucky_window"] = int(getattr(args, "lucky_window", 24))
        return params
    if key == "thusnelda":
        return {"placeholder_level": int(getattr(args, "placeholder_level", 1))}
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


def _as_float_values(raw: Any) -> list[float]:
    if isinstance(raw, str):
        tokens = [x.strip() for x in raw.split(",")]
        values = [float(x) for x in tokens if x]
    elif isinstance(raw, (list, tuple)):
        values = [float(x) for x in raw]
    else:
        values = [float(raw)]
    unique = sorted({round(float(v), 12) for v in values})
    return [float(v) for v in unique]


def _build_float_grid(lo: float, hi: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("step must be > 0")
    d_lo = Decimal(str(lo))
    d_hi = Decimal(str(hi))
    if d_lo > d_hi:
        d_lo, d_hi = d_hi, d_lo
    d_step = Decimal(str(step))
    span = d_hi - d_lo
    slots = int((span / d_step).to_integral_value(rounding=ROUND_FLOOR))
    out = [float(d_lo + d_step * i) for i in range(slots + 1)]
    if out and out[-1] < float(d_hi):
        out.append(float(d_hi))
    elif not out:
        out = [float(d_lo), float(d_hi)]
    unique = sorted({round(float(v), 12) for v in out})
    return [float(v) for v in unique]


def _suggest_float_param(
    trial: Any,
    overrides: Dict[str, Any],
    name: str,
    default_min: float,
    default_max: float,
) -> float:
    if name in overrides:
        return float(overrides[name])
    min_key = f"{name}_min"
    max_key = f"{name}_max"
    step_key = f"{name}_step"
    values_key = f"{name}_values"
    lo, hi = _get_float_range(overrides, min_key, max_key, default_min, default_max)
    if values_key in overrides:
        values = _as_float_values(overrides[values_key])
        return float(trial.suggest_categorical(name, values))
    if step_key in overrides:
        values = _build_float_grid(lo, hi, float(overrides[step_key]))
        return float(trial.suggest_categorical(name, values))
    if lo == hi:
        return float(lo)
    return float(trial.suggest_float(name, lo, hi))


def suggest_params(trial: Any, strategy_name: str, search_overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    key = strategy_name.strip().lower()
    overrides = search_overrides or {}
    override = SUGGEST_PARAMS_OVERRIDES.get(key)
    if override is not None and key not in (
        "dorothy",
        "dorothy_hub",
        "dorothy_legacy",
        "elphaba",
        "elphaba_hub",
        "ha_trend",
        "masha",
        "louise",
        "louise_lucky",
        "anti_louise",
        "anti_louise_lucky",
        "thusnelda",
        "sma_cross",
    ):
        return override(trial, overrides)
    if key in ("dorothy", "dorothy_hub"):
        md_min, md_max = _get_float_range(overrides, "margin_drop_factor_min", "margin_drop_factor_max", 0.001, 0.02)
        r_min, r_max = _get_int_range(overrides, "max_rungs_min", "max_rungs_max", 2, 10)
        return {
            "profit_factor": _suggest_float_param(trial, overrides, "profit_factor", 0.005, 0.08),
            "margin_drop_factor": _suggest_float_param(
                trial,
                overrides,
                "margin_drop_factor",
                md_min,
                md_max,
            ),
            "quote_order_qty_usdt": float(overrides.get("quote_order_qty_usdt", 8.0)),
            "max_rungs": int(trial.suggest_int("max_rungs", r_min, r_max)),
        }
    if key == "dorothy_legacy":
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
    if key == "masha":
        fast_min, fast_max = _get_int_range(overrides, "fast_min", "fast_max", 5, 30)
        slow_min, slow_max = _get_int_range(overrides, "slow_min", "slow_max", 20, 120)
        tp_min, tp_max = _get_float_range(overrides, "target_profit_pct_min", "target_profit_pct_max", 0.2, 5.0)
        sl_min, sl_max = _get_float_range(overrides, "stop_loss_pct_min", "stop_loss_pct_max", 1.0, 10.0)
        pb_min, pb_max = _get_float_range(overrides, "pullback_factor_min", "pullback_factor_max", 0.001, 0.03)
        params = {
            "fast": int(trial.suggest_int("fast", fast_min, fast_max)),
            "slow": int(trial.suggest_int("slow", slow_min, slow_max)),
            "quote_order_qty_usdt": 8.0,
            "take_profit_pct": float(trial.suggest_float("take_profit_pct", tp_min, tp_max)),
            "stop_loss_pct": float(trial.suggest_float("stop_loss_pct", sl_min, sl_max)),
            "pullback_factor": float(trial.suggest_float("pullback_factor", pb_min, pb_max)),
        }
        if params["fast"] >= params["slow"]:
            params["_invalid"] = True
        return params
    if key in ("louise", "louise_lucky"):
        tp_min, tp_max = _get_float_range(overrides, "target_profit_pct_min", "target_profit_pct_max", 0.2, 5.0)
        md_min, md_max = _get_float_range(overrides, "margin_drop_factor_min", "margin_drop_factor_max", 0.001, 0.03)
        params = {
            "target_profit_pct": float(trial.suggest_float("target_profit_pct", tp_min, tp_max)),
            "margin_drop_factor": float(trial.suggest_float("margin_drop_factor", md_min, md_max)),
            "quote_order_qty_usdt": 8.0,
        }
        if key.endswith("_lucky"):
            params["lucky_window"] = int(trial.suggest_int("lucky_window", 8, 72))
        return params
    if key in ("anti_louise", "anti_louise_lucky"):
        tp_min, tp_max = _get_float_range(overrides, "target_profit_pct_min", "target_profit_pct_max", 0.2, 5.0)
        mr_min, mr_max = _get_float_range(overrides, "margin_rise_factor_min", "margin_rise_factor_max", 0.001, 0.03)
        params = {
            "target_profit_pct": float(trial.suggest_float("target_profit_pct", tp_min, tp_max)),
            "margin_rise_factor": float(trial.suggest_float("margin_rise_factor", mr_min, mr_max)),
            "quote_order_qty_usdt": 8.0,
        }
        if key.endswith("_lucky"):
            params["lucky_window"] = int(trial.suggest_int("lucky_window", 8, 72))
        return params
    if key == "thusnelda":
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

