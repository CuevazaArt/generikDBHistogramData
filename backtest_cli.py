"""Simple terminal interface for backtesting and optimization."""
import argparse
import datetime as dt
import json
from pathlib import Path
import sqlite3
from typing import Any, Optional

from backtest.engine import EngineConfig
from backtest.optimize import optimize_strategy
from backtest.plots import export_study_summary_table, export_summary, plot_equity_and_drawdown, plot_signal_histograms, plot_trials
from backtest.registry import get_strategy, list_strategy_names, params_from_cli
from backtest.runner import execute_and_persist
from backtest.storage import (
    list_runs,
    run_equity_curve,
    run_descriptor,
    run_signal_events,
    study_trials,
    summarize_run,
    top_trials,
    trial_objectives,
)
from db import init_db

MENU_SETTINGS_PATH = Path(".backtest_menu_settings.json")


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


def _load_menu_settings() -> dict:
    default = {
        "active_strategy": "dorothy",
        "last_tested_strategy": "",
        "last_tested_run_id": None,
        "last_symbol": "BTCUSDT",
        "last_interval": "1h",
        "last_study": "sma_opt",
        "last_trials": 30,
        "last_n_jobs": 2,
    }
    try:
        if MENU_SETTINGS_PATH.exists():
            data = json.loads(MENU_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                active = str(data.get("active_strategy", default["active_strategy"])).strip().lower()
                if active in list_strategy_names(include_aliases=False):
                    default["active_strategy"] = active
                default["last_tested_strategy"] = str(data.get("last_tested_strategy", "")).strip().lower()
                last_run_id = data.get("last_tested_run_id")
                default["last_tested_run_id"] = int(last_run_id) if isinstance(last_run_id, int) else None
                default["last_symbol"] = str(data.get("last_symbol", default["last_symbol"])).strip() or default["last_symbol"]
                default["last_interval"] = str(data.get("last_interval", default["last_interval"])).strip() or default["last_interval"]
                default["last_study"] = str(data.get("last_study", default["last_study"])).strip() or default["last_study"]
                trials = data.get("last_trials")
                n_jobs = data.get("last_n_jobs")
                if isinstance(trials, int) and trials > 0:
                    default["last_trials"] = trials
                if isinstance(n_jobs, int) and n_jobs > 0:
                    default["last_n_jobs"] = n_jobs
    except Exception:
        pass
    return default


def _save_menu_settings(settings: dict) -> None:
    try:
        MENU_SETTINGS_PATH.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        print(f"No se pudo guardar configuración de menú: {exc}")


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
            print(f"Valor invalido. Debe ser entero >= {min_value}.")


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
            print(f"Valor invalido. Debe ser numero >= {min_value}.")


def _print_metrics(metrics: dict) -> None:
    print("\n--- Metricas ---")
    for key in sorted(metrics.keys()):
        value = metrics[key]
        if isinstance(value, float):
            print(f"- {key}: {value:.6f}")
        else:
            print(f"- {key}: {value}")


def _print_kv_block(title: str, data: dict[str, Any]) -> None:
    print(f"\n--- {title} ---")
    if not data:
        print("- (vacio)")
        return
    width = max(len(str(k)) for k in data.keys())
    for key in sorted(data.keys()):
        value = data[key]
        if isinstance(value, float):
            value_text = f"{value:.6f}"
        elif isinstance(value, (dict, list)):
            value_text = json.dumps(value, ensure_ascii=False)
        else:
            value_text = str(value)
        print(f"- {str(key).ljust(width)} : {value_text}")


def _print_paths(title: str, paths: dict[str, str]) -> None:
    print(f"\n--- {title} ---")
    if not paths:
        print("- No hay archivos para mostrar.")
        return
    for key in sorted(paths.keys()):
        print(f"- {key}: {paths[key]}")


def _preview_params(params: dict[str, Any], max_len: int = 140) -> str:
    text = json.dumps(params, ensure_ascii=False, sort_keys=True)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _last_run_row(db_path: str):
    rows = list_runs(db_path, limit=1)
    return rows[0] if rows else None


def _best_trial_snapshot(db_path: str) -> Optional[dict]:
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
    params = {}
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


def _run_strategy_snapshot(db_path: str, run_id: int) -> Optional[dict]:
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute("SELECT strategy_name, symbol, interval, config_json FROM bt_runs WHERE run_id=?", (int(run_id),))
        row = cur.fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    strategy_params = {}
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


def _best_run_setup_for_strategy(db_path: str, strategy_name: str) -> Optional[dict]:
    if not strategy_name:
        return None
    try:
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            """
            SELECT r.run_id, r.symbol, r.interval, r.config_json, m.metric_value
            FROM bt_runs r
            JOIN bt_metrics m ON m.run_id = r.run_id
            WHERE r.strategy_name=? AND m.metric_name='total_return'
            ORDER BY m.metric_value DESC, r.run_id DESC
            LIMIT 1
            """,
            (strategy_name,),
        )
        row = cur.fetchone()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    params = {}
    try:
        cfg = json.loads(row[3]) if row[3] else {}
        params = cfg.get("strategy", {}) if isinstance(cfg, dict) else {}
    except Exception:
        params = {}
    return {
        "run_id": int(row[0]),
        "symbol": str(row[1]),
        "interval": str(row[2]),
        "params": params,
        "total_return": float(row[4]),
    }


def _print_menu_dashboard(db_path: str, settings: dict) -> None:
    available = list_strategy_names(include_aliases=False)
    active = str(settings.get("active_strategy", "dorothy"))
    latest_run = _last_run_row(db_path)
    latest_strategy = str(latest_run[1]).lower() if latest_run else ""
    latest_run_id = int(latest_run[0]) if latest_run else None
    last_tested = str(settings.get("last_tested_strategy", "")).lower()
    if not last_tested:
        last_tested = latest_strategy

    print("\n=== Bots disponibles ===")
    for name in available:
        marks = []
        if name == active:
            marks.append("activo")
        if name == last_tested:
            marks.append("ultimo_test")
        label = f"{name} [{' | '.join(marks)}]" if marks else name
        print(f"- {label}")

    if latest_run_id is not None:
        print(f"Ultimo run: id={latest_run_id}, strategy={latest_strategy}")
    else:
        print("Ultimo run: sin ejecuciones")

    best_run = _best_run_setup_for_strategy(db_path, last_tested)
    if best_run:
        params_preview = _preview_params(best_run["params"])
        print(
            "Setup mejor ponderado (bot ultimo testeado): "
            f"run={best_run['run_id']} strategy={last_tested} "
            f"return={best_run['total_return']:.6f} "
            f"{best_run['symbol']} {best_run['interval']} params={params_preview}"
        )
        return

    best_trial = _best_trial_snapshot(db_path)
    if best_trial:
        params_preview = _preview_params(best_trial["params"])
        print(
            "Setup mejor ponderado (global): "
            f"study={best_trial['study_name']} trial={best_trial['trial_number']} "
            f"objective={best_trial['objective']:.6f} params={params_preview}"
        )
        return

    if latest_run_id is not None:
        run_cfg = _run_strategy_snapshot(db_path, latest_run_id)
        if run_cfg:
            params_preview = _preview_params(run_cfg["params"])
            print(
                "Setup de referencia (ultimo run): "
                f"{run_cfg['strategy_name']} {run_cfg['symbol']} {run_cfg['interval']} "
                f"params={params_preview}"
            )
            return

    print("Setup mejor ponderado: no disponible aun.")


def _select_active_strategy(settings: dict) -> None:
    available = list_strategy_names(include_aliases=False)
    active = settings.get("active_strategy", "dorothy")
    print("\n=== Bots/Estrategias disponibles ===")
    for idx, name in enumerate(available, start=1):
        mark = "*" if name == active else " "
        print(f"{mark} {idx}) {name}")
    choice = input("Selecciona por número o nombre (Enter = cancelar): ").strip().lower()
    if not choice:
        print("Sin cambios.")
        return
    selected = None
    if choice.isdigit():
        i = int(choice)
        if 1 <= i <= len(available):
            selected = available[i - 1]
    elif choice in available:
        selected = choice
    if not selected:
        print("Selección inválida.")
        return
    settings["active_strategy"] = selected
    _save_menu_settings(settings)
    print(f"Estrategia activa guardada: {selected}")


def _ask_strategy(default: str) -> str:
    available = list_strategy_names(include_aliases=False)
    prompt = f"Estrategia [{ '|'.join(available) }] (def {default}): "
    selected = (input(prompt).strip() or default).lower()
    if selected not in available:
        print(f"Estrategia invalida: {selected}. Se usa {default}.")
        return default
    return selected


def _build_optuna_search_overrides(strategy: str) -> dict[str, Any]:
    print("\nConfigurar rango de busqueda Optuna")
    print("Enter = usar rangos por defecto.")
    custom = _ask_text("Personalizar rangos? (y/n)", "n").strip().lower()
    if custom not in ("y", "yes", "s", "si"):
        return {}

    overrides: dict[str, Any] = {}
    if strategy in ("dorothy", "dorothy_legacy"):
        overrides["profit_factor_min"] = _ask_float("profit_factor min", 0.005, 0.0)
        overrides["profit_factor_max"] = _ask_float("profit_factor max", 0.08, 0.0)
        overrides["margin_drop_factor_min"] = _ask_float("margin_drop_factor min", 0.001, 0.0)
        overrides["margin_drop_factor_max"] = _ask_float("margin_drop_factor max", 0.02, 0.0)
        overrides["max_rungs_min"] = _ask_int("max_rungs min", 2, 1)
        overrides["max_rungs_max"] = _ask_int("max_rungs max", 10, 1)
    elif strategy == "elphaba":
        overrides["profit_factor_min"] = _ask_float("profit_factor min", 0.005, 0.0)
        overrides["profit_factor_max"] = _ask_float("profit_factor max", 0.08, 0.0)
        overrides["margin_rise_factor_min"] = _ask_float("margin_rise_factor min", 0.005, 0.0)
        overrides["margin_rise_factor_max"] = _ask_float("margin_rise_factor max", 0.05, 0.0)
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
        overrides["target_profit_pct_min"] = _ask_float("target_profit_pct min", 0.2, 0.0)
        overrides["target_profit_pct_max"] = _ask_float("target_profit_pct max", 5.0, 0.0)
        overrides["stop_loss_pct_min"] = _ask_float("stop_loss_pct min", 1.0, 0.0)
        overrides["stop_loss_pct_max"] = _ask_float("stop_loss_pct max", 10.0, 0.0)
        overrides["pullback_factor_min"] = _ask_float("pullback_factor min", 0.001, 0.0)
        overrides["pullback_factor_max"] = _ask_float("pullback_factor max", 0.03, 0.0)
    elif strategy in ("louise", "louise_lucky"):
        overrides["target_profit_pct_min"] = _ask_float("target_profit_pct min", 0.2, 0.0)
        overrides["target_profit_pct_max"] = _ask_float("target_profit_pct max", 5.0, 0.0)
        overrides["margin_drop_factor_min"] = _ask_float("margin_drop_factor min", 0.001, 0.0)
        overrides["margin_drop_factor_max"] = _ask_float("margin_drop_factor max", 0.03, 0.0)
    elif strategy in ("anti_louise", "anti_louise_lucky"):
        overrides["target_profit_pct_min"] = _ask_float("target_profit_pct min", 0.2, 0.0)
        overrides["target_profit_pct_max"] = _ask_float("target_profit_pct max", 5.0, 0.0)
        overrides["margin_rise_factor_min"] = _ask_float("margin_rise_factor min", 0.001, 0.0)
        overrides["margin_rise_factor_max"] = _ask_float("margin_rise_factor max", 0.03, 0.0)
    elif strategy == "thusnelda":
        pass
    else:
        print("Esta estrategia no requiere rango custom por ahora.")
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
    print("\n=== Run terminado ===")
    print(f"run_id: {result.run_id}")
    print(f"strategy: {args.strategy}")
    print(f"symbol/interval: {args.symbol} {args.interval}")
    _print_metrics(result.metrics)
    return result


def _optimize(args: argparse.Namespace):
    strategy_cls = get_strategy(args.strategy)
    search_overrides = getattr(args, "search_overrides", None) or _extract_optuna_overrides_from_args(args)
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
    study = optimize_strategy(
        db_path=args.db,
        study_name=args.study,
        strategy_cls=strategy_cls,
        base_config=cfg,
        trials=args.trials,
        n_jobs=args.n_jobs,
        timeout=args.timeout,
        search_overrides=search_overrides,
    )
    print("\n=== Optimizacion completa ===")
    print(f"study: {args.study}")
    print(f"strategy: {args.strategy}")
    print(f"symbol/interval: {args.symbol} {args.interval}")
    print(f"trials: {args.trials} | n_jobs: {args.n_jobs} | timeout_sec: {args.timeout}")
    _print_kv_block("Rangos usados", search_overrides)
    print(f"best_value: {study.best_value:.6f}")
    _print_kv_block("Best params", dict(study.best_params))
    return study


def _show(args: argparse.Namespace) -> None:
    if args.run_id is not None:
        summary = summarize_run(args.db, run_id=args.run_id, events_limit=args.events_limit)
        desc = run_descriptor(args.db, run_id=args.run_id) or {}
        if desc:
            desc["first_event_iso_utc"] = _ms_to_iso(desc.get("first_event_time"))
            desc["last_event_iso_utc"] = _ms_to_iso(desc.get("last_event_time"))
        print(f"\n=== Resumen run_id={args.run_id} ===")
        if desc:
            _print_kv_block("Descriptor", desc)
        _print_metrics(summary["metrics"])
        print("\n--- Eventos recientes ---")
        for e in summary["recent_events"]:
            print(e)
        return
    print("Ultimos runs:")
    rows = list_runs(args.db, limit=args.limit)
    if not rows:
        print("- Sin runs registrados.")
    else:
        for r in rows:
            run_id, strategy, symbol, interval, status, created_at, ended_at = r
            print(
                f"- run_id={run_id} strategy={strategy} {symbol}/{interval} "
                f"status={status} created={created_at} ended={ended_at}"
            )
    if args.study:
        print(f"\nTop trials ({args.study}):")
        rows = top_trials(args.db, study_name=args.study, limit=10)
        if not rows:
            print("- Sin trials.")
        for t in rows:
            trial_id, trial_number, state, objective, params_json, started_at, finished_at = t
            params_preview = str(params_json)[:120] if params_json else "{}"
            objective_text = f"{float(objective):.6f}" if objective is not None else "None"
            print(
                f"- trial_id={trial_id} number={trial_number} state={state} "
                f"objective={objective_text} started={started_at} finished={finished_at} "
                f"params={params_preview}"
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
        _print_paths("Resumen final de estudio", summary_paths)
        if p:
            print(f"- Grafica de trials: {p}")


def _menu(db_path: str) -> None:
    settings = _load_menu_settings()
    while True:
        active_strategy = settings.get("active_strategy", "dorothy")
        _print_menu_dashboard(db_path, settings)
        print("\n=== Backtesting Terminal ===")
        print(f"Estrategia activa: {active_strategy}")
        print("1) Ejecutar backtest")
        print("2) Optimizar estrategia (Optuna)")
        print("3) Ver runs/trials")
        print("4) Graficar run")
        print("5) Ver/cargar bot de trade")
        print("6) Salir")
        choice = input("Opcion: ").strip()
        if choice == "1":
            symbol = _ask_text("Simbolo", str(settings.get("last_symbol", "BTCUSDT"))).upper()
            interval = _ask_text("Intervalo", str(settings.get("last_interval", "1h")))
            strategy = _ask_strategy(active_strategy)
            base_payload = {
                "db": db_path,
                "strategy": strategy,
                "symbol": symbol,
                "interval": interval,
                "start_ts": None,
                "end_ts": None,
                "initial_cash": 10000.0,
                "fee_rate": 0.001,
                "slippage_bps": 2.0,
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
            }
            if strategy == "sma_cross":
                base_payload["fast"] = _ask_int("SMA fast", 10, min_value=1)
                base_payload["slow"] = _ask_int("SMA slow", 30, min_value=2)
            elif strategy in ("dorothy", "dorothy_legacy"):
                base_payload["profit_factor"] = _ask_float("profit_factor", 0.05, min_value=0.0)
                base_payload["margin_drop_factor"] = _ask_float("margin_drop_factor", 0.004, min_value=0.0)
                base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
                base_payload["max_rungs"] = _ask_int("max_rungs", 5, min_value=1)
                if strategy == "dorothy_legacy":
                    base_payload["min_order_notional"] = _ask_float("min_order_notional", 6.0, min_value=0.0)
                    base_payload["max_order_notional"] = _ask_float("max_order_notional", 10.0, min_value=0.0)
                    base_payload["max_active_orders"] = _ask_int("max_active_orders", 200, min_value=1)
            elif strategy == "elphaba":
                base_payload["profit_factor"] = _ask_float("profit_factor", 0.05, min_value=0.0)
                base_payload["margin_rise_factor"] = _ask_float("margin_rise_factor", 0.03, min_value=0.0)
                base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
                base_payload["max_rungs"] = _ask_int("max_rungs", 5, min_value=1)
            elif strategy == "ha_trend":
                tm = _ask_text("trend_mode (both|long|short)", "both").lower()
                if tm not in ("both", "long", "short"):
                    tm = "both"
                base_payload["trend_mode"] = tm
                base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
            elif strategy == "masha":
                base_payload["fast"] = _ask_int("fast", 9, min_value=2)
                base_payload["slow"] = _ask_int("slow", 34, min_value=3)
                base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
                base_payload["take_profit_pct"] = _ask_float("take_profit_pct", 1.5, min_value=0.0)
                base_payload["stop_loss_pct"] = _ask_float("stop_loss_pct", 4.0, min_value=0.0)
                base_payload["pullback_factor"] = _ask_float("pullback_factor", 0.006, min_value=0.0)
            elif strategy in ("louise", "louise_lucky"):
                base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
                base_payload["target_profit_pct"] = _ask_float("target_profit_pct", 1.5, min_value=0.0)
                base_payload["margin_drop_factor"] = _ask_float("margin_drop_factor", 0.004, min_value=0.0)
                if strategy.endswith("_lucky"):
                    base_payload["lucky_window"] = _ask_int("lucky_window", 24, min_value=3)
            elif strategy in ("anti_louise", "anti_louise_lucky"):
                base_payload["quote_order_qty_usdt"] = _ask_float("quote_order_qty_usdt", 8.0, min_value=0.1)
                base_payload["target_profit_pct"] = _ask_float("target_profit_pct", 1.5, min_value=0.0)
                base_payload["margin_rise_factor"] = _ask_float("margin_rise_factor", 0.004, min_value=0.0)
                if strategy.endswith("_lucky"):
                    base_payload["lucky_window"] = _ask_int("lucky_window", 24, min_value=3)
            elif strategy == "thusnelda":
                base_payload["placeholder_level"] = _ask_int("placeholder_level", 1, min_value=1)
            else:
                print(f"Estrategia desconocida: {strategy}")
                continue
            args = argparse.Namespace(
                **base_payload
            )
            result = _run_once(args)
            if result is not None:
                settings["last_tested_strategy"] = strategy
                settings["last_tested_run_id"] = int(result.run_id) if result.run_id is not None else None
                settings["last_symbol"] = symbol
                settings["last_interval"] = interval
                _save_menu_settings(settings)
        elif choice == "2":
            symbol = _ask_text("Simbolo", str(settings.get("last_symbol", "BTCUSDT"))).upper()
            interval = _ask_text("Intervalo", str(settings.get("last_interval", "1h")))
            strategy = _ask_strategy(active_strategy)
            study = _ask_text("Study name", str(settings.get("last_study", "sma_opt")))
            trials = _ask_int("Trials", int(settings.get("last_trials", 30)), min_value=1)
            jobs = _ask_int("n_jobs CPU", int(settings.get("last_n_jobs", 2)), min_value=1)
            timeout = _ask_int("timeout sec (0=sin limite)", 0, min_value=0)
            search_overrides = _build_optuna_search_overrides(strategy)
            _print_kv_block(
                "Configuracion de optimizacion",
                {
                    "strategy": strategy,
                    "symbol": symbol,
                    "interval": interval,
                    "study_name": study,
                    "trials": trials,
                    "n_jobs": jobs,
                    "timeout_sec": timeout,
                },
            )
            _print_kv_block("Rangos custom Optuna", search_overrides)
            args = argparse.Namespace(
                db=db_path,
                strategy=strategy,
                symbol=symbol,
                interval=interval,
                start_ts=None,
                end_ts=None,
                initial_cash=10000.0,
                fee_rate=0.001,
                slippage_bps=2.0,
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
                stop_loss_pct=4.0,
                pullback_factor=0.006,
                lucky_window=24,
                search_overrides=search_overrides,
            )
            _optimize(args)
            settings["last_tested_strategy"] = strategy
            settings["last_symbol"] = symbol
            settings["last_interval"] = interval
            settings["last_study"] = study
            settings["last_trials"] = int(trials)
            settings["last_n_jobs"] = int(jobs)
            _save_menu_settings(settings)
        elif choice == "3":
            _show(argparse.Namespace(db=db_path, run_id=None, limit=20, study=None, events_limit=25))
        elif choice == "4":
            run_id = int(input("run_id: ").strip())
            _plot(
                argparse.Namespace(
                    db=db_path,
                    run_id=run_id,
                    study=None,
                    output_dir="reports",
                    signal_bins=30,
                )
            )
        elif choice == "5":
            _select_active_strategy(settings)
        elif choice == "6":
            break
        else:
            print("Opcion invalida.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtesting + optimization terminal")
    parser.add_argument("--db", default="klines.db")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run")
    p_run.add_argument(
        "--strategy",
        default="dorothy",
        choices=(
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
        ),
    )
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
    p_opt.add_argument(
        "--strategy",
        default="dorothy",
        choices=(
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
        ),
    )
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
    elif args.cmd == "show":
        _show(args)
    elif args.cmd == "plot":
        _plot(args)
    else:
        _menu(args.db)


if __name__ == "__main__":
    main()

