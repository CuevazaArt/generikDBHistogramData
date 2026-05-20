"""Terminal interface for backtesting and Optuna optimization."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from backtest.engine import EngineConfig
from backtest.optimize import (
    AVAILABLE_OBJECTIVE_METRICS,
    AVAILABLE_SAMPLERS,
    OptimizationConfig,
    optimize_strategy,
)
from backtest.plots import (
    export_study_summary_table,
    export_summary,
    plot_equity_and_drawdown,
    plot_signal_histograms,
    plot_trials,
)
from backtest.registry import get_strategy, list_strategy_names, params_from_cli
from backtest.runner import execute_and_persist
from backtest.storage import (
    list_runs,
    run_descriptor,
    run_equity_curve,
    run_signal_events,
    study_trials,
    summarize_run,
    top_trials,
    trial_objectives,
)
from backtest.walkforward import WalkForwardResult, run_walkforward
from db import init_db

MENU_SETTINGS_PATH = Path(".backtest_menu_settings.json")

PCT_METRIC_KEYS = {"total_return", "max_drawdown", "win_rate", "ulcer_index"}
MONEY_METRIC_KEYS = {"initial_cash", "final_equity"}

METRIC_DESCRIPTIONS: dict[str, str] = {
    "initial_cash": "capital inicial",
    "final_equity": "equity final",
    "total_return": "retorno total sobre capital",
    "max_drawdown": "peor caida pico-valle",
    "sharpe": "rendimiento vs volatilidad total",
    "sortino": "rendimiento vs volatilidad solo a la baja",
    "calmar": "retorno / max drawdown",
    "ulcer_index": "intensidad y duracion de drawdowns",
    "win_rate": "% de trades ganadores",
    "profit_factor": "ganancia bruta / perdida bruta",
    "num_trades": "cantidad de trades cerrados",
}


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def _fmt_pct(value: float, decimals: int = 2) -> str:
    return f"{value * 100:.{decimals}f}%"


def _fmt_money(value: float, decimals: int = 2) -> str:
    return f"{value:,.{decimals}f}"


def _fmt_num(value: Any, decimals: int = 6) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        if abs(value) >= 1000:
            return f"{value:,.{decimals}f}"
        return f"{value:.{decimals}f}"
    return str(value)


def _fmt_metric(name: str, value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        if name in PCT_METRIC_KEYS:
            return _fmt_pct(float(value))
        if name in MONEY_METRIC_KEYS:
            return _fmt_money(float(value))
        if isinstance(value, float):
            return _fmt_num(value)
        return str(value)
    return str(value)


def _visible_len(text: str) -> int:
    return len(str(text))


def _print_header(title: str, width: int = 78) -> None:
    line = "=" * width
    centered = f" {title} ".center(width, "=")
    print()
    print(line)
    print(centered)
    print(line)


def _print_section(title: str, width: int = 78) -> None:
    print()
    print(f"-- {title} ".ljust(width, "-"))


def _print_table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    aligns: Optional[Sequence[str]] = None,
    empty_msg: str = "(sin datos)",
) -> None:
    if not rows:
        print(f"   {empty_msg}")
        return
    cols = len(headers)
    norm_rows = [[(str(cell) if cell is not None else "-") for cell in row] for row in rows]
    widths = [_visible_len(h) for h in headers]
    for row in norm_rows:
        for i in range(cols):
            widths[i] = max(widths[i], _visible_len(row[i]))
    aligns = list(aligns or ["left"] * cols)

    def _fmt_cell(value: str, width: int, align: str) -> str:
        if align == "right":
            return value.rjust(width)
        if align == "center":
            return value.center(width)
        return value.ljust(width)

    border = "+-" + "-+-".join("-" * w for w in widths) + "-+"
    header_line = "| " + " | ".join(_fmt_cell(h, widths[i], "center") for i, h in enumerate(headers)) + " |"
    print(border)
    print(header_line)
    print(border)
    for row in norm_rows:
        print("| " + " | ".join(_fmt_cell(row[i], widths[i], aligns[i]) for i in range(cols)) + " |")
    print(border)


def _print_kv(title: str, data: dict[str, Any]) -> None:
    _print_section(title)
    if not data:
        print("   (vacio)")
        return
    rows = []
    for key in sorted(data.keys()):
        value = data[key]
        if isinstance(value, float):
            display = _fmt_num(value)
        elif isinstance(value, (dict, list)):
            display = json.dumps(value, ensure_ascii=False)
        else:
            display = str(value) if value is not None else "-"
        rows.append([str(key), display])
    _print_table(["clave", "valor"], rows, aligns=["left", "left"])


def _print_metrics_table(metrics: dict[str, Any]) -> None:
    _print_section("Metricas")
    preferred_order = [
        "initial_cash",
        "final_equity",
        "total_return",
        "max_drawdown",
        "calmar",
        "ulcer_index",
        "sharpe",
        "sortino",
        "win_rate",
        "profit_factor",
        "num_trades",
    ]
    seen: set[str] = set()
    ordered_keys: list[str] = []
    for key in preferred_order:
        if key in metrics:
            ordered_keys.append(key)
            seen.add(key)
    for key in sorted(metrics.keys()):
        if key not in seen:
            ordered_keys.append(key)
    rows = []
    for key in ordered_keys:
        rows.append(
            [
                key,
                _fmt_metric(key, metrics[key]),
                METRIC_DESCRIPTIONS.get(key, "-"),
            ]
        )
    _print_table(
        ["metrica", "valor", "descripcion"],
        rows,
        aligns=["left", "right", "left"],
    )


def _print_paths(title: str, paths: dict[str, str]) -> None:
    _print_section(title)
    if not paths:
        print("   No hay archivos para mostrar.")
        return
    rows = [[k, paths[k]] for k in sorted(paths.keys())]
    _print_table(["tipo", "ruta"], rows, aligns=["left", "left"])


def _preview_params(params: dict[str, Any], max_len: int = 110) -> str:
    text = json.dumps(params, ensure_ascii=False, sort_keys=True)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------


def _ask_text(prompt: str, default: str) -> str:
    v = input(f"{prompt} [{default}]: ").strip()
    return v or default


def _ask_int(prompt: str, default: int, min_value: int = 1) -> int:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return int(default)
        try:
            value = int(raw)
            if value < min_value:
                raise ValueError()
            return value
        except Exception:
            print(f"   Valor invalido. Debe ser entero >= {min_value}.")


def _ask_float(prompt: str, default: float, min_value: float = 0.0) -> float:
    while True:
        raw = input(f"{prompt} [{default}]: ").strip()
        if not raw:
            return float(default)
        try:
            value = float(raw)
            if value < min_value:
                raise ValueError()
            return value
        except Exception:
            print(f"   Valor invalido. Debe ser numero >= {min_value}.")


def _ask_choice(prompt: str, choices: Sequence[str], default: str) -> str:
    options = list(choices)
    while True:
        raw = input(f"{prompt} {list(options)} [{default}]: ").strip().lower()
        if not raw:
            return default
        if raw in options:
            return raw
        print(f"   Opcion invalida. Permitidas: {', '.join(options)}.")


# ---------------------------------------------------------------------------
# CLI parsing helpers
# ---------------------------------------------------------------------------


def _parse_ts(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    return int(v)


def _ms_to_iso(v: Optional[int]) -> Optional[str]:
    if v is None:
        return None
    try:
        return dt.datetime.fromtimestamp(v / 1000.0, tz=dt.timezone.utc).isoformat()
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Persistent menu settings
# ---------------------------------------------------------------------------


_DEFAULT_SETTINGS: dict[str, Any] = {
    "active_strategy": "dorothy",
    "last_tested_strategy": "",
    "last_tested_run_id": None,
    "last_symbol": "BTCUSDT",
    "last_interval": "1h",
    "last_study": "sma_opt",
    "last_trials": 30,
    "last_n_jobs": 2,
    "last_objective_metric": "total_return",
    "last_direction": "maximize",
    "last_sampler": "tpe",
    "last_seed": 0,
    "last_initial_cash": 10000.0,
    "last_fee_rate": 0.001,
    "last_slippage_bps": 2.0,
}


def _load_menu_settings() -> dict[str, Any]:
    out = dict(_DEFAULT_SETTINGS)
    try:
        if not MENU_SETTINGS_PATH.exists():
            return out
        data = json.loads(MENU_SETTINGS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return out
        for key, default in _DEFAULT_SETTINGS.items():
            if key not in data:
                continue
            value = data[key]
            if isinstance(default, bool) and isinstance(value, bool):
                out[key] = value
            elif isinstance(default, int) and isinstance(value, int) and not isinstance(value, bool):
                out[key] = value
            elif isinstance(default, float) and isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = float(value)
            elif isinstance(default, str) and isinstance(value, str):
                out[key] = value
            elif default is None:
                out[key] = value
        if out["active_strategy"] not in list_strategy_names(include_aliases=False):
            out["active_strategy"] = _DEFAULT_SETTINGS["active_strategy"]
        if out["last_objective_metric"] not in AVAILABLE_OBJECTIVE_METRICS:
            out["last_objective_metric"] = "total_return"
        if out["last_direction"] not in ("maximize", "minimize"):
            out["last_direction"] = "maximize"
        if out["last_sampler"] not in AVAILABLE_SAMPLERS:
            out["last_sampler"] = "tpe"
    except Exception:
        return dict(_DEFAULT_SETTINGS)
    return out


def _save_menu_settings(settings: dict[str, Any]) -> None:
    try:
        MENU_SETTINGS_PATH.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"   No se pudo guardar configuracion de menu: {exc}")


# ---------------------------------------------------------------------------
# DB query helpers for dashboard
# ---------------------------------------------------------------------------


def _last_run_row(db_path: str):
    rows = list_runs(db_path, limit=1)
    return rows[0] if rows else None


def _best_trial_snapshot(db_path: str) -> Optional[dict[str, Any]]:
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT study_name, trial_number, objective, params_json
            FROM bt_trials
            WHERE objective IS NOT NULL AND state='COMPLETE'
            ORDER BY objective DESC
            LIMIT 1
            """
        )
        row = cur.fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    try:
        params = json.loads(row[3]) if row[3] else {}
    except Exception:
        params = {}
    return {
        "study_name": str(row[0]),
        "trial_number": int(row[1]),
        "objective": float(row[2]),
        "params": params,
    }


def _run_strategy_snapshot(db_path: str, run_id: int) -> Optional[dict[str, Any]]:
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT strategy_name, symbol, interval, config_json FROM bt_runs WHERE run_id=?",
            (int(run_id),),
        )
        row = cur.fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    try:
        cfg = json.loads(row[3]) if row[3] else {}
        strategy_params = cfg.get("strategy", {}) if isinstance(cfg, dict) else {}
    except Exception:
        strategy_params = {}
    return {
        "strategy_name": str(row[0]),
        "symbol": str(row[1]),
        "interval": str(row[2]),
        "params": strategy_params,
    }


def _top_runs_for_strategy(
    db_path: str, strategy_name: str, metric_name: str = "total_return", limit: int = 3
) -> list[dict[str, Any]]:
    if not strategy_name:
        return []
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT r.run_id, r.symbol, r.interval, r.config_json, m.metric_value
            FROM bt_runs r
            JOIN bt_metrics m ON m.run_id = r.run_id
            WHERE r.strategy_name=? AND m.metric_name=?
            ORDER BY m.metric_value DESC, r.run_id DESC
            LIMIT ?
            """,
            (strategy_name, metric_name, int(limit)),
        )
        rows = cur.fetchall()
        conn.close()
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            cfg = json.loads(row[3]) if row[3] else {}
            params = cfg.get("strategy", {}) if isinstance(cfg, dict) else {}
        except Exception:
            params = {}
        out.append(
            {
                "run_id": int(row[0]),
                "symbol": str(row[1]),
                "interval": str(row[2]),
                "params": params,
                "metric": float(row[4]),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def _print_bots_table(
    db_path: str, available: Iterable[str], active: str, last_tested: str
) -> None:
    rows = []
    for name in available:
        best = _top_runs_for_strategy(db_path, name, "total_return", limit=1)
        best_return = _fmt_pct(best[0]["metric"]) if best else "-"
        symbol_iv = f"{best[0]['symbol']}/{best[0]['interval']}" if best else "-"
        marks = []
        if name == active:
            marks.append("activo")
        if name == last_tested:
            marks.append("ultimo_test")
        rows.append(
            [
                name,
                " | ".join(marks) if marks else "-",
                best_return,
                symbol_iv,
            ]
        )
    _print_section("Bots disponibles")
    _print_table(
        ["bot", "estado", "mejor_return", "mejor_par"],
        rows,
        aligns=["left", "left", "right", "left"],
        empty_msg="(no hay bots registrados)",
    )


def _print_top_setups(db_path: str, strategy_name: str, metric: str = "total_return") -> None:
    rows = _top_runs_for_strategy(db_path, strategy_name, metric, limit=3)
    _print_section(f"Top 3 setups del bot activo ({strategy_name}, metric={metric})")
    if not rows:
        print("   (sin runs registrados para este bot)")
        return
    table_rows = []
    for r in rows:
        table_rows.append(
            [
                r["run_id"],
                f"{r['symbol']}/{r['interval']}",
                _fmt_pct(r["metric"]),
                _preview_params(r["params"]),
            ]
        )
    _print_table(
        ["run_id", "par", "return", "params"],
        table_rows,
        aligns=["right", "left", "right", "left"],
    )


def _print_menu_dashboard(db_path: str, settings: dict[str, Any]) -> None:
    available = list_strategy_names(include_aliases=False)
    active = str(settings.get("active_strategy", "dorothy"))
    latest_run = _last_run_row(db_path)
    latest_strategy = str(latest_run[1]).lower() if latest_run else ""
    latest_run_id = int(latest_run[0]) if latest_run else None
    last_tested = str(settings.get("last_tested_strategy", "")).lower()
    if not last_tested:
        last_tested = latest_strategy

    _print_header(f"Backtesting Terminal | db={db_path}")
    _print_bots_table(db_path, available, active, last_tested)
    _print_top_setups(db_path, active)

    _print_section("Ultimo run")
    if latest_run_id is None:
        print("   sin ejecuciones registradas")
    else:
        print(f"   run_id={latest_run_id} | strategy={latest_strategy}")

    _print_section("Defaults persistidos")
    _print_table(
        ["clave", "valor"],
        [
            ["last_symbol", settings.get("last_symbol")],
            ["last_interval", settings.get("last_interval")],
            ["last_study", settings.get("last_study")],
            ["last_trials", settings.get("last_trials")],
            ["last_n_jobs", settings.get("last_n_jobs")],
            ["objective_metric", settings.get("last_objective_metric")],
            ["direction", settings.get("last_direction")],
            ["sampler", settings.get("last_sampler")],
            ["seed", settings.get("last_seed")],
            ["initial_cash", _fmt_money(float(settings.get("last_initial_cash", 0.0)))],
            ["fee_rate", settings.get("last_fee_rate")],
            ["slippage_bps", settings.get("last_slippage_bps")],
        ],
        aligns=["left", "left"],
    )


# ---------------------------------------------------------------------------
# Strategy selection
# ---------------------------------------------------------------------------


def _select_active_strategy(settings: dict[str, Any]) -> None:
    available = list_strategy_names(include_aliases=False)
    active = settings.get("active_strategy", "dorothy")
    _print_section("Bots/Estrategias disponibles")
    rows = [["*" if name == active else " ", str(idx), name] for idx, name in enumerate(available, start=1)]
    _print_table(["sel", "#", "bot"], rows, aligns=["center", "right", "left"])
    choice = input("Selecciona por numero o nombre (Enter = cancelar): ").strip().lower()
    if not choice:
        print("   Sin cambios.")
        return
    selected = None
    if choice.isdigit():
        i = int(choice)
        if 1 <= i <= len(available):
            selected = available[i - 1]
    elif choice in available:
        selected = choice
    if not selected:
        print("   Seleccion invalida.")
        return
    settings["active_strategy"] = selected
    _save_menu_settings(settings)
    print(f"   Estrategia activa guardada: {selected}")


def _ask_strategy(default: str) -> str:
    available = list_strategy_names(include_aliases=False)
    print("   Estrategias disponibles: " + ", ".join(available))
    selected = (input(f"Estrategia (def {default}): ").strip() or default).lower()
    if selected not in available:
        print(f"   Estrategia invalida: {selected}. Se usa {default}.")
        return default
    return selected


# ---------------------------------------------------------------------------
# Optuna search-space helpers
# ---------------------------------------------------------------------------


def _build_optuna_search_overrides(strategy: str) -> dict[str, Any]:
    _print_section("Rangos de busqueda Optuna")
    print("   Enter = usar rangos por defecto del bot.")
    custom = _ask_choice("Personalizar rangos?", ["y", "n"], "n")
    if custom != "y":
        return {}

    overrides: dict[str, Any] = {}
    if strategy in ("dorothy", "dorothy_legacy"):
        overrides["profit_factor_min"] = _ask_float("profit_factor min", 0.005)
        overrides["profit_factor_max"] = _ask_float("profit_factor max", 0.08)
        overrides["margin_drop_factor_min"] = _ask_float("margin_drop_factor min", 0.001)
        overrides["margin_drop_factor_max"] = _ask_float("margin_drop_factor max", 0.02)
        overrides["max_rungs_min"] = _ask_int("max_rungs min", 2, 1)
        overrides["max_rungs_max"] = _ask_int("max_rungs max", 10, 1)
    elif strategy == "elphaba":
        overrides["profit_factor_min"] = _ask_float("profit_factor min", 0.005)
        overrides["profit_factor_max"] = _ask_float("profit_factor max", 0.08)
        overrides["margin_rise_factor_min"] = _ask_float("margin_rise_factor min", 0.005)
        overrides["margin_rise_factor_max"] = _ask_float("margin_rise_factor max", 0.05)
        overrides["max_rungs_min"] = _ask_int("max_rungs min", 2, 1)
        overrides["max_rungs_max"] = _ask_int("max_rungs max", 10, 1)
    elif strategy == "sma_cross":
        overrides["fast_min"] = _ask_int("fast min", 5, 1)
        overrides["fast_max"] = _ask_int("fast max", 40, 1)
        overrides["slow_min"] = _ask_int("slow min", 20, 2)
        overrides["slow_max"] = _ask_int("slow max", 120, 2)
    elif strategy == "masha":
        overrides["fast_min"] = _ask_int("fast min", 5, 1)
        overrides["fast_max"] = _ask_int("fast max", 30, 1)
        overrides["slow_min"] = _ask_int("slow min", 20, 2)
        overrides["slow_max"] = _ask_int("slow max", 120, 2)
        overrides["target_profit_pct_min"] = _ask_float("target_profit_pct min", 0.2)
        overrides["target_profit_pct_max"] = _ask_float("target_profit_pct max", 5.0)
        overrides["stop_loss_pct_min"] = _ask_float("stop_loss_pct min", 1.0)
        overrides["stop_loss_pct_max"] = _ask_float("stop_loss_pct max", 10.0)
        overrides["pullback_factor_min"] = _ask_float("pullback_factor min", 0.001)
        overrides["pullback_factor_max"] = _ask_float("pullback_factor max", 0.03)
    elif strategy in ("louise", "louise_lucky"):
        overrides["target_profit_pct_min"] = _ask_float("target_profit_pct min", 0.2)
        overrides["target_profit_pct_max"] = _ask_float("target_profit_pct max", 5.0)
        overrides["margin_drop_factor_min"] = _ask_float("margin_drop_factor min", 0.001)
        overrides["margin_drop_factor_max"] = _ask_float("margin_drop_factor max", 0.03)
    elif strategy in ("anti_louise", "anti_louise_lucky"):
        overrides["target_profit_pct_min"] = _ask_float("target_profit_pct min", 0.2)
        overrides["target_profit_pct_max"] = _ask_float("target_profit_pct max", 5.0)
        overrides["margin_rise_factor_min"] = _ask_float("margin_rise_factor min", 0.001)
        overrides["margin_rise_factor_max"] = _ask_float("margin_rise_factor max", 0.03)
    else:
        print("   Esta estrategia no expone rangos custom (placeholder o sin parametros).")
    return overrides


def _extract_optuna_overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    keys = (
        "profit_factor_min",
        "profit_factor_max",
        "margin_drop_factor_min",
        "margin_drop_factor_max",
        "margin_rise_factor_min",
        "margin_rise_factor_max",
        "max_rungs_min",
        "max_rungs_max",
        "fast_min",
        "fast_max",
        "slow_min",
        "slow_max",
        "target_profit_pct_min",
        "target_profit_pct_max",
        "stop_loss_pct_min",
        "stop_loss_pct_max",
        "pullback_factor_min",
        "pullback_factor_max",
    )
    out: dict[str, Any] = {}
    for key in keys:
        value = getattr(args, key, None)
        if value is not None:
            out[key] = value
    return out


# ---------------------------------------------------------------------------
# Core commands
# ---------------------------------------------------------------------------


def _run_once(args: argparse.Namespace):
    strategy_cls = get_strategy(args.strategy)
    strategy_params = params_from_cli(args, args.strategy)
    cfg = EngineConfig(
        db_path=args.db,
        symbol=args.symbol,
        interval=args.interval,
        start_ts=_parse_ts(args.start_ts),
        end_ts=_parse_ts(args.end_ts),
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        use_heikin_ashi=args.heikin_ashi,
        sma_fast=int(strategy_params.get("fast", 10)),
        sma_slow=int(strategy_params.get("slow", 30)),
    )
    result = execute_and_persist(
        config=cfg,
        strategy_cls=strategy_cls,
        strategy_params=strategy_params,
    )
    _print_header(f"Run terminado | run_id={result.run_id}")
    _print_kv(
        "Resumen del run",
        {
            "strategy": args.strategy,
            "symbol": args.symbol,
            "interval": args.interval,
            "initial_cash": args.initial_cash,
            "fee_rate": args.fee_rate,
            "slippage_bps": args.slippage_bps,
            "heikin_ashi": bool(args.heikin_ashi),
        },
    )
    _print_kv("Parametros de estrategia", strategy_params)
    _print_metrics_table(result.metrics)
    return result


def _optimize(args: argparse.Namespace):
    strategy_cls = get_strategy(args.strategy)
    search_overrides = getattr(args, "search_overrides", None) or _extract_optuna_overrides_from_args(args)
    objective_metric = getattr(args, "objective_metric", "total_return") or "total_return"
    direction = getattr(args, "direction", "maximize") or "maximize"
    sampler = getattr(args, "sampler", "tpe") or "tpe"
    seed = getattr(args, "seed", None)
    seed_val = int(seed) if seed not in (None, 0) else None

    cfg = EngineConfig(
        db_path=args.db,
        symbol=args.symbol,
        interval=args.interval,
        start_ts=_parse_ts(args.start_ts),
        end_ts=_parse_ts(args.end_ts),
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        use_heikin_ashi=args.heikin_ashi,
    )
    opt_cfg = OptimizationConfig(
        objective_metric=objective_metric,
        direction=direction,
        sampler=sampler,
        seed=seed_val,
    )

    _print_header(f"Optimizacion Optuna | study={args.study}")
    _print_kv(
        "Setup",
        {
            "strategy": args.strategy,
            "symbol": args.symbol,
            "interval": args.interval,
            "trials": args.trials,
            "n_jobs": args.n_jobs,
            "timeout_sec": args.timeout,
            "objective_metric": opt_cfg.objective_metric,
            "direction": opt_cfg.direction,
            "sampler": opt_cfg.sampler,
            "seed": opt_cfg.seed,
        },
    )
    _print_kv("Rangos custom", search_overrides)

    study = optimize_strategy(
        db_path=args.db,
        study_name=args.study,
        strategy_cls=strategy_cls,
        base_config=cfg,
        trials=args.trials,
        n_jobs=args.n_jobs,
        timeout=args.timeout,
        search_overrides=search_overrides,
        optimization=opt_cfg,
    )
    _print_section("Resultado")
    print(f"   best_value ({opt_cfg.objective_metric}): {study.best_value:.6f}")
    _print_kv("Best params", dict(study.best_params))
    return study


def _walkforward(args: argparse.Namespace) -> WalkForwardResult:
    strategy_cls = get_strategy(args.strategy)
    cfg = EngineConfig(
        db_path=args.db,
        symbol=args.symbol,
        interval=args.interval,
        start_ts=_parse_ts(args.start_ts),
        end_ts=_parse_ts(args.end_ts),
        initial_cash=args.initial_cash,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        use_heikin_ashi=getattr(args, "heikin_ashi", False),
    )
    opt = OptimizationConfig(
        objective_metric=getattr(args, "objective_metric", "total_return"),
        direction=getattr(args, "direction", "maximize"),
        sampler=getattr(args, "sampler", "tpe"),
        seed=(int(args.seed) if getattr(args, "seed", 0) not in (None, 0) else None),
    )
    train_pct = float(getattr(args, "train_pct", 0.7))

    _print_header(
        f"Walk-forward | strategy={args.strategy} | study={args.study} | train_pct={train_pct:.0%}"
    )
    _print_kv(
        "Setup",
        {
            "strategy": args.strategy,
            "symbol": args.symbol,
            "interval": args.interval,
            "trials": args.trials,
            "n_jobs": args.n_jobs,
            "train_pct": train_pct,
            "objective_metric": opt.objective_metric,
            "direction": opt.direction,
            "sampler": opt.sampler,
            "seed": opt.seed,
        },
    )

    result = run_walkforward(
        db_path=args.db,
        study_name=args.study,
        strategy_cls=strategy_cls,
        base_config=cfg,
        trials=args.trials,
        n_jobs=args.n_jobs,
        train_pct=train_pct,
        timeout=getattr(args, "timeout", None),
        search_overrides=getattr(args, "search_overrides", None) or {},
        optimization=opt,
    )

    diff = result.test_metric - result.train_metric
    if result.metric_name in PCT_METRIC_KEYS:
        train_display = _fmt_pct(result.train_metric)
        test_display = _fmt_pct(result.test_metric)
        diff_display = _fmt_pct(diff)
    else:
        train_display = _fmt_num(result.train_metric)
        test_display = _fmt_num(result.test_metric)
        diff_display = _fmt_num(diff)

    _print_section("Resultado walk-forward")
    _print_table(
        ["fase", "run_id", result.metric_name],
        [
            ["train (in-sample)", result.train_run_id, train_display],
            ["validation (out-of-sample)", result.test_run_id, test_display],
            ["delta (val - train)", "-", diff_display],
        ],
        aligns=["left", "right", "right"],
    )
    _print_kv("Best params", result.best_params)

    if result.metric_name in {"max_drawdown", "ulcer_index"}:
        # Lower is better for these.
        verdict = "consistente" if result.test_metric <= result.train_metric * 1.2 else "posible sobreajuste"
    else:
        verdict = "consistente" if result.test_metric >= result.train_metric * 0.5 else "posible sobreajuste"
    print(f"   Veredicto rapido: {verdict}")
    return result


def _show(args: argparse.Namespace) -> None:
    if args.run_id is not None:
        summary = summarize_run(args.db, run_id=args.run_id, events_limit=args.events_limit)
        desc = run_descriptor(args.db, run_id=args.run_id) or {}
        if desc:
            desc["first_event_iso_utc"] = _ms_to_iso(desc.get("first_event_time"))
            desc["last_event_iso_utc"] = _ms_to_iso(desc.get("last_event_time"))
        _print_header(f"Resumen run_id={args.run_id}")
        if desc:
            _print_kv("Descriptor", desc)
        _print_metrics_table(summary["metrics"])
        _print_section("Eventos recientes")
        for e in summary["recent_events"]:
            print(f"   {e}")
        return

    _print_header(f"Ultimos runs (limit={args.limit})")
    rows = list_runs(args.db, limit=args.limit)
    if not rows:
        print("   Sin runs registrados.")
    else:
        table_rows = []
        for r in rows:
            run_id, strategy, symbol, interval, status, created_at, ended_at = r
            table_rows.append([run_id, strategy, f"{symbol}/{interval}", status, created_at, ended_at])
        _print_table(
            ["run_id", "strategy", "par", "status", "created_at", "ended_at"],
            table_rows,
            aligns=["right", "left", "left", "left", "left", "left"],
        )
    if args.study:
        _print_header(f"Top trials | study={args.study}")
        rows = top_trials(args.db, study_name=args.study, limit=10)
        if not rows:
            print("   Sin trials.")
        else:
            tab = []
            for t in rows:
                trial_id, trial_number, state, objective, params_json, started_at, finished_at = t
                obj = f"{float(objective):.6f}" if objective is not None else "-"
                tab.append(
                    [
                        trial_id,
                        trial_number,
                        state,
                        obj,
                        started_at,
                        finished_at,
                        _preview_params(json.loads(params_json) if params_json else {}, max_len=70),
                    ]
                )
            _print_table(
                ["trial_id", "number", "state", "objective", "started_at", "finished_at", "params"],
                tab,
                aligns=["right", "right", "left", "right", "left", "left", "left"],
            )


def _plot(args: argparse.Namespace) -> None:
    if args.run_id is not None:
        eq_rows = run_equity_curve(args.db, run_id=args.run_id)
        paths = plot_equity_and_drawdown(eq_rows, output_dir=args.output_dir, run_id=args.run_id)
        signal_rows = run_signal_events(args.db, run_id=args.run_id)
        signal_paths = plot_signal_histograms(
            signal_rows=signal_rows,
            output_dir=args.output_dir,
            run_id=args.run_id,
            bins=args.signal_bins,
        )
        metrics = summarize_run(args.db, run_id=args.run_id)["metrics"]
        descriptor = run_descriptor(args.db, run_id=args.run_id) or {}
        descriptor["first_event_iso_utc"] = _ms_to_iso(descriptor.get("first_event_time"))
        descriptor["last_event_iso_utc"] = _ms_to_iso(descriptor.get("last_event_time"))
        export = export_summary(args.output_dir, f"run_{args.run_id}", metrics, eq_rows, descriptor=descriptor)
        _print_header(f"Reportes generados | run_id={args.run_id}")
        _print_paths("Graficas y archivos exportados", {**paths, **signal_paths, **export})
    if args.study:
        objective_rows = trial_objectives(args.db, study_name=args.study, limit=1000)
        p = plot_trials(
            trial_rows=objective_rows,
            output_dir=args.output_dir,
            study_name=args.study,
        )
        summary_paths = export_study_summary_table(
            output_dir=args.output_dir,
            study_name=args.study,
            trials=study_trials(args.db, study_name=args.study, limit=2000),
        )
        _print_header(f"Reportes del estudio | study={args.study}")
        _print_paths("Resumen final de estudio", summary_paths)
        if p:
            print(f"   Grafica de trials: {p}")


# ---------------------------------------------------------------------------
# Menu loop
# ---------------------------------------------------------------------------


def _ask_engine_basics(settings: dict[str, Any]) -> tuple[str, str, float, float, float]:
    symbol = _ask_text("Simbolo", str(settings.get("last_symbol", "BTCUSDT"))).upper()
    interval = _ask_text("Intervalo", str(settings.get("last_interval", "1h")))
    initial_cash = _ask_float("initial_cash USDT", float(settings.get("last_initial_cash", 10000.0)))
    fee_rate = _ask_float("fee_rate", float(settings.get("last_fee_rate", 0.001)))
    slippage_bps = _ask_float("slippage_bps", float(settings.get("last_slippage_bps", 2.0)))
    return symbol, interval, initial_cash, fee_rate, slippage_bps


def _menu_run_backtest(db_path: str, settings: dict[str, Any], active_strategy: str) -> None:
    symbol, interval, initial_cash, fee_rate, slippage_bps = _ask_engine_basics(settings)
    strategy = _ask_strategy(active_strategy)
    base_payload: dict[str, Any] = {
        "db": db_path,
        "strategy": strategy,
        "symbol": symbol,
        "interval": interval,
        "start_ts": None,
        "end_ts": None,
        "initial_cash": initial_cash,
        "fee_rate": fee_rate,
        "slippage_bps": slippage_bps,
        "heikin_ashi": False,
        "fast": 10,
        "slow": 30,
        "profit_factor": 0.05,
        "margin_drop_factor": 0.004,
        "margin_rise_factor": 0.03,
        "quote_order_qty_usdt": 8.0,
        "min_order_notional": 6.0,
        "max_order_notional": 10.0,
        "max_active_orders": 200,
        "max_rungs": 5,
        "trend_mode": "both",
        "placeholder_level": 1,
        "target_profit_pct": 1.5,
        "take_profit_pct": 1.5,
        "stop_loss_pct": 4.0,
        "pullback_factor": 0.006,
        "lucky_window": 24,
    }

    if strategy == "sma_cross":
        base_payload["fast"] = _ask_int("SMA fast", 10, min_value=1)
        base_payload["slow"] = _ask_int("SMA slow", 30, min_value=2)
    elif strategy in ("dorothy", "dorothy_legacy"):
        base_payload["profit_factor"] = _ask_float("profit_factor", 0.05)
        base_payload["margin_drop_factor"] = _ask_float("margin_drop_factor", 0.004)
        base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
        base_payload["max_rungs"] = _ask_int("max_rungs", 5, min_value=1)
        if strategy == "dorothy_legacy":
            base_payload["min_order_notional"] = _ask_float("min_order_notional", 6.0)
            base_payload["max_order_notional"] = _ask_float("max_order_notional", 10.0)
            base_payload["max_active_orders"] = _ask_int("max_active_orders", 200, min_value=1)
    elif strategy == "elphaba":
        base_payload["profit_factor"] = _ask_float("profit_factor", 0.05)
        base_payload["margin_rise_factor"] = _ask_float("margin_rise_factor", 0.03)
        base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
        base_payload["max_rungs"] = _ask_int("max_rungs", 5, min_value=1)
    elif strategy == "ha_trend":
        base_payload["trend_mode"] = _ask_choice("trend_mode", ["both", "long", "short"], "both")
        base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
    elif strategy == "masha":
        base_payload["fast"] = _ask_int("fast", 9, min_value=2)
        base_payload["slow"] = _ask_int("slow", 34, min_value=3)
        base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
        base_payload["take_profit_pct"] = _ask_float("take_profit_pct", 1.5)
        base_payload["stop_loss_pct"] = _ask_float("stop_loss_pct", 4.0)
        base_payload["pullback_factor"] = _ask_float("pullback_factor", 0.006)
    elif strategy in ("louise", "louise_lucky"):
        base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
        base_payload["target_profit_pct"] = _ask_float("target_profit_pct", 1.5)
        base_payload["margin_drop_factor"] = _ask_float("margin_drop_factor", 0.004)
        if strategy.endswith("_lucky"):
            base_payload["lucky_window"] = _ask_int("lucky_window", 24, min_value=3)
    elif strategy in ("anti_louise", "anti_louise_lucky"):
        base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
        base_payload["target_profit_pct"] = _ask_float("target_profit_pct", 1.5)
        base_payload["margin_rise_factor"] = _ask_float("margin_rise_factor", 0.004)
        if strategy.endswith("_lucky"):
            base_payload["lucky_window"] = _ask_int("lucky_window", 24, min_value=3)
    elif strategy == "thusnelda":
        base_payload["placeholder_level"] = _ask_int("placeholder_level", 1, min_value=1)
    else:
        print(f"   Estrategia desconocida: {strategy}")
        return

    args = argparse.Namespace(**base_payload)
    result = _run_once(args)
    if result is not None:
        settings["last_tested_strategy"] = strategy
        settings["last_tested_run_id"] = int(result.run_id) if result.run_id is not None else None
        settings["last_symbol"] = symbol
        settings["last_interval"] = interval
        settings["last_initial_cash"] = float(initial_cash)
        settings["last_fee_rate"] = float(fee_rate)
        settings["last_slippage_bps"] = float(slippage_bps)
        _save_menu_settings(settings)


def _menu_optimize(db_path: str, settings: dict[str, Any], active_strategy: str) -> None:
    symbol, interval, initial_cash, fee_rate, slippage_bps = _ask_engine_basics(settings)
    strategy = _ask_strategy(active_strategy)
    study = _ask_text("Study name", str(settings.get("last_study", "sma_opt")))
    trials = _ask_int("Trials", int(settings.get("last_trials", 30)), min_value=1)
    jobs = _ask_int("n_jobs CPU", int(settings.get("last_n_jobs", 2)), min_value=1)
    timeout = _ask_int("timeout sec (0=sin limite)", 0, min_value=0)
    objective_metric = _ask_choice(
        "objective_metric",
        list(AVAILABLE_OBJECTIVE_METRICS),
        str(settings.get("last_objective_metric", "total_return")),
    )
    direction = _ask_choice(
        "direction",
        ["maximize", "minimize"],
        str(settings.get("last_direction", "maximize")),
    )
    sampler = _ask_choice(
        "sampler",
        list(AVAILABLE_SAMPLERS),
        str(settings.get("last_sampler", "tpe")),
    )
    seed = _ask_int("seed (0=aleatorio)", int(settings.get("last_seed", 0)), min_value=0)
    search_overrides = _build_optuna_search_overrides(strategy)

    _print_kv(
        "Configuracion de optimizacion",
        {
            "strategy": strategy,
            "symbol": symbol,
            "interval": interval,
            "study_name": study,
            "trials": trials,
            "n_jobs": jobs,
            "timeout_sec": timeout,
            "objective_metric": objective_metric,
            "direction": direction,
            "sampler": sampler,
            "seed": seed,
            "initial_cash": initial_cash,
            "fee_rate": fee_rate,
            "slippage_bps": slippage_bps,
        },
    )
    _print_kv("Rangos custom Optuna", search_overrides)

    args = argparse.Namespace(
        db=db_path,
        strategy=strategy,
        symbol=symbol,
        interval=interval,
        start_ts=None,
        end_ts=None,
        initial_cash=initial_cash,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        heikin_ashi=False,
        study=study,
        trials=trials,
        n_jobs=jobs,
        timeout=(None if timeout <= 0 else timeout),
        quote_order_qty_usdt=8.0,
        min_order_notional=6.0,
        max_order_notional=10.0,
        max_active_orders=200,
        max_rungs=5,
        margin_drop_factor=0.004,
        margin_rise_factor=0.03,
        profit_factor=0.05,
        fast=10,
        slow=30,
        trend_mode="both",
        placeholder_level=1,
        target_profit_pct=1.5,
        take_profit_pct=1.5,
        stop_loss_pct=4.0,
        pullback_factor=0.006,
        lucky_window=24,
        objective_metric=objective_metric,
        direction=direction,
        sampler=sampler,
        seed=seed,
        search_overrides=search_overrides,
    )
    _optimize(args)
    settings["last_tested_strategy"] = strategy
    settings["last_symbol"] = symbol
    settings["last_interval"] = interval
    settings["last_study"] = study
    settings["last_trials"] = int(trials)
    settings["last_n_jobs"] = int(jobs)
    settings["last_objective_metric"] = objective_metric
    settings["last_direction"] = direction
    settings["last_sampler"] = sampler
    settings["last_seed"] = int(seed)
    settings["last_initial_cash"] = float(initial_cash)
    settings["last_fee_rate"] = float(fee_rate)
    settings["last_slippage_bps"] = float(slippage_bps)
    _save_menu_settings(settings)


def _menu_walkforward(db_path: str, settings: dict[str, Any], active_strategy: str) -> None:
    symbol, interval, initial_cash, fee_rate, slippage_bps = _ask_engine_basics(settings)
    strategy = _ask_strategy(active_strategy)
    study = _ask_text(
        "Study name (se reusa para optimizar la parte train)",
        f"{strategy}_wf",
    )
    trials = _ask_int("Trials de optimizacion", int(settings.get("last_trials", 30)), min_value=1)
    jobs = _ask_int("n_jobs CPU", int(settings.get("last_n_jobs", 2)), min_value=1)
    train_pct_int = _ask_int("Porcentaje train (10-90)", 70, min_value=10)
    train_pct = max(0.1, min(0.9, train_pct_int / 100.0))
    objective_metric = _ask_choice(
        "objective_metric",
        list(AVAILABLE_OBJECTIVE_METRICS),
        str(settings.get("last_objective_metric", "total_return")),
    )
    direction = _ask_choice(
        "direction",
        ["maximize", "minimize"],
        str(settings.get("last_direction", "maximize")),
    )
    sampler = _ask_choice(
        "sampler",
        list(AVAILABLE_SAMPLERS),
        str(settings.get("last_sampler", "tpe")),
    )
    seed = _ask_int("seed (0=aleatorio)", int(settings.get("last_seed", 0)), min_value=0)

    args = argparse.Namespace(
        db=db_path,
        strategy=strategy,
        symbol=symbol,
        interval=interval,
        start_ts=None,
        end_ts=None,
        initial_cash=initial_cash,
        fee_rate=fee_rate,
        slippage_bps=slippage_bps,
        heikin_ashi=False,
        study=study,
        trials=trials,
        n_jobs=jobs,
        timeout=None,
        train_pct=train_pct,
        objective_metric=objective_metric,
        direction=direction,
        sampler=sampler,
        seed=seed,
        search_overrides={},
    )
    _walkforward(args)
    settings["last_tested_strategy"] = strategy
    settings["last_symbol"] = symbol
    settings["last_interval"] = interval
    settings["last_study"] = study
    settings["last_trials"] = int(trials)
    settings["last_n_jobs"] = int(jobs)
    settings["last_objective_metric"] = objective_metric
    settings["last_direction"] = direction
    settings["last_sampler"] = sampler
    settings["last_seed"] = int(seed)
    settings["last_initial_cash"] = float(initial_cash)
    settings["last_fee_rate"] = float(fee_rate)
    settings["last_slippage_bps"] = float(slippage_bps)
    _save_menu_settings(settings)


def _menu(db_path: str) -> None:
    settings = _load_menu_settings()
    while True:
        active_strategy = settings.get("active_strategy", "dorothy")
        _print_menu_dashboard(db_path, settings)
        _print_section("Menu principal")
        print("   1) Ejecutar backtest")
        print("   2) Optimizar estrategia (Optuna)")
        print("   3) Walk-forward (train + validacion)")
        print("   4) Ver runs / trials")
        print("   5) Graficar run")
        print("   6) Ver / cargar bot de trade")
        print("   7) Salir")
        choice = input("Opcion: ").strip()
        if choice == "1":
            _menu_run_backtest(db_path, settings, active_strategy)
        elif choice == "2":
            _menu_optimize(db_path, settings, active_strategy)
        elif choice == "3":
            _menu_walkforward(db_path, settings, active_strategy)
        elif choice == "4":
            _show(argparse.Namespace(db=db_path, run_id=None, limit=20, study=None, events_limit=25))
        elif choice == "5":
            run_id = _ask_int("run_id", 1, min_value=1)
            _plot(
                argparse.Namespace(
                    db=db_path,
                    run_id=run_id,
                    study=None,
                    output_dir="reports",
                    signal_bins=30,
                )
            )
        elif choice == "6":
            _select_active_strategy(settings)
        elif choice == "7":
            print("   Hasta luego.")
            break
        else:
            print("   Opcion invalida.")


# ---------------------------------------------------------------------------
# Argparse plumbing
# ---------------------------------------------------------------------------


_STRATEGY_CHOICES = (
    "dorothy",
    "elphaba",
    "ha_trend",
    "sma_cross",
    "dorothy_legacy",
    "dorothy_hub",
    "elphaba_hub",
    "masha",
    "thusnelda",
    "louise",
    "anti_louise",
    "louise_lucky",
    "anti_louise_lucky",
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtesting + optimization terminal")
    parser.add_argument("--db", default="klines.db")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run")
    p_run.add_argument("--strategy", default="dorothy", choices=_STRATEGY_CHOICES)
    p_run.add_argument("--symbol", required=True)
    p_run.add_argument("--interval", required=True)
    p_run.add_argument("--start_ts")
    p_run.add_argument("--end_ts")
    p_run.add_argument("--initial_cash", type=float, default=10000.0)
    p_run.add_argument("--fee_rate", type=float, default=0.001)
    p_run.add_argument("--slippage_bps", type=float, default=2.0)
    p_run.add_argument("--heikin_ashi", action="store_true")
    p_run.add_argument("--fast", type=int, default=10)
    p_run.add_argument("--slow", type=int, default=30)
    p_run.add_argument("--profit_factor", type=float, default=0.05)
    p_run.add_argument("--margin_drop_factor", type=float, default=0.004)
    p_run.add_argument("--quote_order_qty_usdt", type=float, default=8.0)
    p_run.add_argument("--min_order_notional", type=float, default=6.0)
    p_run.add_argument("--max_order_notional", type=float, default=10.0)
    p_run.add_argument("--max_active_orders", type=int, default=200)
    p_run.add_argument("--max_rungs", type=int, default=5)
    p_run.add_argument("--margin_rise_factor", type=float, default=0.03)
    p_run.add_argument("--trend_mode", default="both", choices=("both", "long", "short"))
    p_run.add_argument("--placeholder_level", type=int, default=1)
    p_run.add_argument("--target_profit_pct", type=float, default=1.5)
    p_run.add_argument("--take_profit_pct", type=float, default=1.5)
    p_run.add_argument("--stop_loss_pct", type=float, default=4.0)
    p_run.add_argument("--pullback_factor", type=float, default=0.006)
    p_run.add_argument("--lucky_window", type=int, default=24)

    p_opt = sub.add_parser("optimize")
    p_opt.add_argument("--strategy", default="dorothy", choices=_STRATEGY_CHOICES)
    p_opt.add_argument("--symbol", required=True)
    p_opt.add_argument("--interval", required=True)
    p_opt.add_argument("--study", default="sma_opt")
    p_opt.add_argument("--start_ts")
    p_opt.add_argument("--end_ts")
    p_opt.add_argument("--initial_cash", type=float, default=10000.0)
    p_opt.add_argument("--fee_rate", type=float, default=0.001)
    p_opt.add_argument("--slippage_bps", type=float, default=2.0)
    p_opt.add_argument("--heikin_ashi", action="store_true")
    p_opt.add_argument("--trials", type=int, default=30)
    p_opt.add_argument("--n_jobs", type=int, default=2)
    p_opt.add_argument("--timeout", type=int)
    p_opt.add_argument("--objective_metric", default="total_return", choices=tuple(AVAILABLE_OBJECTIVE_METRICS))
    p_opt.add_argument("--direction", default="maximize", choices=("maximize", "minimize"))
    p_opt.add_argument("--sampler", default="tpe", choices=tuple(AVAILABLE_SAMPLERS))
    p_opt.add_argument("--seed", type=int, default=0)
    p_opt.add_argument("--quote_order_qty_usdt", type=float, default=8.0)
    p_opt.add_argument("--min_order_notional", type=float, default=6.0)
    p_opt.add_argument("--max_order_notional", type=float, default=10.0)
    p_opt.add_argument("--max_active_orders", type=int, default=200)
    p_opt.add_argument("--max_rungs", type=int, default=5)
    p_opt.add_argument("--profit_factor", type=float, default=0.05)
    p_opt.add_argument("--margin_drop_factor", type=float, default=0.004)
    p_opt.add_argument("--margin_rise_factor", type=float, default=0.03)
    p_opt.add_argument("--fast", type=int, default=10)
    p_opt.add_argument("--slow", type=int, default=30)
    p_opt.add_argument("--trend_mode", default="both", choices=("both", "long", "short"))
    p_opt.add_argument("--placeholder_level", type=int, default=1)
    p_opt.add_argument("--target_profit_pct", type=float, default=1.5)
    p_opt.add_argument("--take_profit_pct", type=float, default=1.5)
    p_opt.add_argument("--stop_loss_pct", type=float, default=4.0)
    p_opt.add_argument("--pullback_factor", type=float, default=0.006)
    p_opt.add_argument("--lucky_window", type=int, default=24)
    p_opt.add_argument("--profit_factor_min", type=float)
    p_opt.add_argument("--profit_factor_max", type=float)
    p_opt.add_argument("--margin_drop_factor_min", type=float)
    p_opt.add_argument("--margin_drop_factor_max", type=float)
    p_opt.add_argument("--margin_rise_factor_min", type=float)
    p_opt.add_argument("--margin_rise_factor_max", type=float)
    p_opt.add_argument("--max_rungs_min", type=int)
    p_opt.add_argument("--max_rungs_max", type=int)
    p_opt.add_argument("--fast_min", type=int)
    p_opt.add_argument("--fast_max", type=int)
    p_opt.add_argument("--slow_min", type=int)
    p_opt.add_argument("--slow_max", type=int)
    p_opt.add_argument("--target_profit_pct_min", type=float)
    p_opt.add_argument("--target_profit_pct_max", type=float)
    p_opt.add_argument("--stop_loss_pct_min", type=float)
    p_opt.add_argument("--stop_loss_pct_max", type=float)
    p_opt.add_argument("--pullback_factor_min", type=float)
    p_opt.add_argument("--pullback_factor_max", type=float)

    p_wf = sub.add_parser("walkforward")
    p_wf.add_argument("--strategy", default="dorothy", choices=_STRATEGY_CHOICES)
    p_wf.add_argument("--symbol", required=True)
    p_wf.add_argument("--interval", required=True)
    p_wf.add_argument("--study", default="wf_study")
    p_wf.add_argument("--start_ts")
    p_wf.add_argument("--end_ts")
    p_wf.add_argument("--initial_cash", type=float, default=10000.0)
    p_wf.add_argument("--fee_rate", type=float, default=0.001)
    p_wf.add_argument("--slippage_bps", type=float, default=2.0)
    p_wf.add_argument("--heikin_ashi", action="store_true")
    p_wf.add_argument("--trials", type=int, default=20)
    p_wf.add_argument("--n_jobs", type=int, default=1)
    p_wf.add_argument("--timeout", type=int)
    p_wf.add_argument("--train_pct", type=float, default=0.7)
    p_wf.add_argument("--objective_metric", default="total_return", choices=tuple(AVAILABLE_OBJECTIVE_METRICS))
    p_wf.add_argument("--direction", default="maximize", choices=("maximize", "minimize"))
    p_wf.add_argument("--sampler", default="tpe", choices=tuple(AVAILABLE_SAMPLERS))
    p_wf.add_argument("--seed", type=int, default=0)

    p_show = sub.add_parser("show")
    p_show.add_argument("--run_id", type=int)
    p_show.add_argument("--limit", type=int, default=20)
    p_show.add_argument("--study")
    p_show.add_argument("--events_limit", type=int, default=25)

    p_plot = sub.add_parser("plot")
    p_plot.add_argument("--run_id", type=int)
    p_plot.add_argument("--study")
    p_plot.add_argument("--output_dir", default="reports")
    p_plot.add_argument("--signal_bins", type=int, default=30)

    sub.add_parser("menu")
    args = parser.parse_args()
    init_db(args.db)
    if args.cmd == "run":
        _run_once(args)
    elif args.cmd == "optimize":
        _optimize(args)
    elif args.cmd == "walkforward":
        _walkforward(args)
    elif args.cmd == "show":
        _show(args)
    elif args.cmd == "plot":
        _plot(args)
    else:
        _menu(args.db)


if __name__ == "__main__":
    main()
