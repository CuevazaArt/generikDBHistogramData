"""Simple terminal interface for backtesting and optimization."""
import argparse
import datetime as dt
import json
import os
from statistics import mean
from typing import Optional

from backtest.cleanup import abort_stale_runs, purge_aborted_run_events
from backtest.engine import EngineConfig
from backtest.guards import ResourceGuardConfig
from backtest.optimize import optimize_strategy
from backtest.report_paths import run_report_dir, study_report_dir, write_manifest
from backtest.resources import detect_resources, explain_recommendation, recommend_n_jobs
from backtest.sweet_spot import SweetSpotConfig, run_sweet_spot_search
from backtest.sweet_spot_report import build_unified_report
from backtest.plots import (
    export_run_integrated_report,
    export_run_bot_summary,
    export_study_optuna_summary,
    export_study_summary_table,
    export_summary,
    plot_equity_and_drawdown,
    plot_fill_activity_heatmap,
    plot_monthly_return_heatmap,
    plot_monthly_return_spectrum,
    plot_optuna_param_heatmap,
    plot_signal_histograms,
    plot_trials,
)
from backtest.registry import get_strategy, params_from_cli
from backtest.runner import execute_and_persist
from backtest.storage import (
    list_runs,
    run_equity_curve,
    run_descriptor,
    run_events,
    run_signal_events,
    study_trials,
    summarize_run,
    top_trials,
    trial_objectives,
)
from db import init_db


DEFAULT_N_JOBS = max(1, os.cpu_count() or 1)


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


def _run_reports_dir(base_output_dir: str, run_id: int) -> str:
    return run_report_dir(base_output_dir, run_id)


def _study_reports_dir(base_output_dir: str, study_name: str) -> str:
    return study_report_dir(base_output_dir, study_name)


def _run_diagnostics(db_path: str, run_id: int) -> dict:
    rows = run_events(db_path, run_id=run_id)
    if not rows:
        return {}
    util_values = []
    max_open = 0
    cur_open = 0
    for _seq, _ts, event_type, _side, cash, equity, payload_json in rows:
        if equity is not None and cash is not None and float(equity) > 0:
            u = 1.0 - (float(cash) / float(equity))
            util_values.append(max(0.0, min(1.0, u)))
        payload = {}
        if payload_json:
            try:
                payload = json.loads(payload_json)
            except Exception:
                payload = {}
        if event_type == "fill":
            if payload.get("active_limits_after") is not None:
                cur_open = int(payload["active_limits_after"])
            elif payload.get("remaining_limits") is not None:
                cur_open = int(payload["remaining_limits"])
        if cur_open > max_open:
            max_open = cur_open
    return {
        "capital_utilization_avg_pct": round((mean(util_values) if util_values else 0.0) * 100.0, 4),
        "capital_utilization_max_pct": round((max(util_values) if util_values else 0.0) * 100.0, 4),
        "max_open_orders_simultaneous": int(max_open),
    }


def _run_once(args: argparse.Namespace) -> None:
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
        loop_seconds=int(args.loop_seconds) if args.loop_seconds is not None else None,
        sma_fast=int(strategy_params.get("fast", 10)),
        sma_slow=int(strategy_params.get("slow", 30)),
    )
    result = execute_and_persist(
        config=cfg,
        strategy_cls=strategy_cls,
        strategy_params=strategy_params,
    )
    print(f"Run terminado. run_id={result.run_id}")
    print(json.dumps(result.metrics, ensure_ascii=False, indent=2))


def _optimize(args: argparse.Namespace) -> None:
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
        use_heikin_ashi=args.heikin_ashi,
    )
    n_jobs = int(args.n_jobs) if int(args.n_jobs) > 0 else DEFAULT_N_JOBS
    study = optimize_strategy(
        db_path=args.db,
        study_name=args.study,
        strategy_cls=strategy_cls,
        base_config=cfg,
        trials=args.trials,
        n_jobs=n_jobs,
        timeout=args.timeout,
    )
    print(f"Optimización completa. best_value={study.best_value:.6f}")
    print(f"best_params={study.best_params}")
    print(f"n_jobs usados: {n_jobs} (cpu_count={DEFAULT_N_JOBS})")


def _show(args: argparse.Namespace) -> None:
    if args.run_id is not None:
        summary = summarize_run(args.db, run_id=args.run_id, events_limit=args.events_limit)
        desc = run_descriptor(args.db, run_id=args.run_id) or {}
        if desc:
            desc["first_event_iso_utc"] = _ms_to_iso(desc.get("first_event_time"))
            desc["last_event_iso_utc"] = _ms_to_iso(desc.get("last_event_time"))
            desc.update(_run_diagnostics(args.db, run_id=args.run_id))
        print(f"Resumen run_id={args.run_id}")
        if desc:
            print("Descriptor:")
            print(json.dumps(desc, ensure_ascii=False, indent=2))
        print(json.dumps(summary["metrics"], ensure_ascii=False, indent=2))
        print("Eventos recientes:")
        for e in summary["recent_events"]:
            print(e)
        return
    print("Últimos runs:")
    for r in list_runs(args.db, limit=args.limit):
        print(r)
    if args.study:
        print(f"Top trials ({args.study}):")
        for t in top_trials(args.db, study_name=args.study, limit=10):
            print(t)


def _plot(args: argparse.Namespace) -> None:
    if args.run_id is not None:
        run_output_dir = _run_reports_dir(args.output_dir, args.run_id)
        write_manifest(
            run_output_dir,
            title=f"Run {args.run_id} deliverables",
            summary="Artifacts generated by backtest_cli plot --run_id.",
        )
        eq_rows = run_equity_curve(args.db, run_id=args.run_id)
        paths = plot_equity_and_drawdown(eq_rows, output_dir=run_output_dir, run_id=args.run_id)
        signal_rows = run_signal_events(args.db, run_id=args.run_id)
        signal_paths = plot_signal_histograms(
            signal_rows=signal_rows,
            output_dir=run_output_dir,
            run_id=args.run_id,
            bins=args.signal_bins,
        )
        metrics = summarize_run(args.db, run_id=args.run_id)["metrics"]
        descriptor = run_descriptor(args.db, run_id=args.run_id) or {}
        descriptor["first_event_iso_utc"] = _ms_to_iso(descriptor.get("first_event_time"))
        descriptor["last_event_iso_utc"] = _ms_to_iso(descriptor.get("last_event_time"))
        descriptor.update(_run_diagnostics(args.db, run_id=args.run_id))
        spectrum_path = plot_monthly_return_spectrum(eq_rows, output_dir=run_output_dir, run_id=args.run_id)
        heatmap_path = plot_monthly_return_heatmap(eq_rows, output_dir=run_output_dir, run_id=args.run_id)
        activity_heatmap = plot_fill_activity_heatmap(
            run_events(args.db, run_id=args.run_id),
            output_dir=run_output_dir,
            run_id=args.run_id,
        )
        export = export_summary(run_output_dir, f"run_{args.run_id}", metrics, eq_rows, descriptor=descriptor)
        bot_summary_path = export_run_bot_summary(
            output_dir=run_output_dir,
            file_stem=f"run_{args.run_id}",
            descriptor=descriptor,
            metrics=metrics,
            bot_description=(
                "Dorothy es una estrategia de acumulacion y descarga por niveles. "
                "Compra cuando el precio cae por debajo de un umbral relativo al anchor "
                "de limite activo, y cierra solo cuando se activan niveles objetivo de venta."
            ),
            optuna_summary=(
                "Optuna se aplico para maximizar total_return explorando combinaciones de "
                "profit_factor y margin_drop_factor, manteniendo fijas las restricciones "
                "operativas (nocional 6-10 USDT y maximo de 200 ordenes abiertas)."
            ),
        )
        graph_catalog = {**paths, **signal_paths}
        if spectrum_path:
            graph_catalog["monthly_return_spectrum"] = spectrum_path
        if heatmap_path:
            graph_catalog["monthly_return_heatmap"] = heatmap_path
        if activity_heatmap:
            graph_catalog["fill_activity_heatmap"] = activity_heatmap
        integrated_report_path = export_run_integrated_report(
            output_dir=run_output_dir,
            file_stem=f"run_{args.run_id}",
            descriptor=descriptor,
            metrics=metrics,
            equity_rows=eq_rows,
            graph_paths=graph_catalog,
        )
        print(f"Gráficas y archivos exportados en: {run_output_dir}")
        extra = {}
        if spectrum_path:
            extra["monthly_return_spectrum"] = spectrum_path
        if heatmap_path:
            extra["monthly_return_heatmap"] = heatmap_path
        if activity_heatmap:
            extra["fill_activity_heatmap"] = activity_heatmap
        extra["bot_summary_md"] = bot_summary_path
        extra["integrated_report_md"] = integrated_report_path
        print({**paths, **signal_paths, **extra, **export})
    if args.study:
        study_output_dir = _study_reports_dir(args.output_dir, args.study)
        write_manifest(
            study_output_dir,
            title=f"Study {args.study} deliverables",
            summary="Artifacts generated by backtest_cli plot --study.",
        )
        objective_rows = trial_objectives(args.db, study_name=args.study, limit=1000)
        p = plot_trials(
            trial_rows=objective_rows,
            output_dir=study_output_dir,
            study_name=args.study,
        )
        summary_paths = export_study_summary_table(
            output_dir=study_output_dir,
            study_name=args.study,
            trials=study_trials(args.db, study_name=args.study, limit=2000),
        )
        param_heatmap = plot_optuna_param_heatmap(
            trials=study_trials(args.db, study_name=args.study, limit=2000),
            output_dir=study_output_dir,
            study_name=args.study,
        )
        summary_payload = {}
        try:
            with open(summary_paths["study_summary_json"], "r", encoding="utf-8") as fh:
                summary_payload = json.load(fh)
        except Exception:
            summary_payload = {}
        optuna_summary_md = export_study_optuna_summary(
            output_dir=study_output_dir,
            study_name=args.study,
            summary_payload=summary_payload,
        )
        print(f"Resumen final de estudio en: {study_output_dir}")
        extra_summary = dict(summary_paths)
        if param_heatmap:
            extra_summary["study_param_heatmap"] = param_heatmap
        extra_summary["optuna_summary_md"] = optuna_summary_md
        print(extra_summary)
        if p:
            print(f"Gráfica de trials: {p}")


def _sweet_spot(args: argparse.Namespace) -> None:
    profile = detect_resources()
    print(_build_separator())
    print("Buscador de seteo dulce - resumen de recursos")
    print(explain_recommendation(args.mode, profile=profile))
    print(_build_separator())

    cfg = SweetSpotConfig(
        db_path=args.db,
        strategy_name=args.strategy,
        symbol=args.symbol,
        interval=args.interval,
        full_start_ts=int(args.start_ts),
        full_end_ts=int(args.end_ts),
        initial_cash=float(args.initial_cash),
        fee_rate=float(args.fee_rate),
        slippage_bps=float(args.slippage_bps),
        use_heikin_ashi=bool(args.heikin_ashi),
        loop_seconds=int(args.loop_seconds) if args.loop_seconds is not None else None,
        coarse_window_pct=float(args.coarse_window_pct),
        coarse_trials=int(args.coarse_trials),
        coarse_mode=args.mode,
        coarse_objective_metric=args.objective_metric,
        coarse_direction=args.direction,
        coarse_sampler=args.sampler,
        coarse_seed=int(args.seed) if args.seed is not None else None,
        focused_top_k=int(args.top_k),
        focused_mode="safe",
        focused_events_mode="full",
        guard_cpu_cap_pct=float(args.guard_cpu_cap_pct),
        guard_ram_cap_pct=float(args.guard_ram_cap_pct),
        guard_sample_sec=float(args.guard_sample_sec),
        guard_high_watermark_windows=int(args.guard_high_windows),
        guard_recover_windows=int(args.guard_recover_windows),
        guard_backoff_sec=float(args.guard_backoff_sec),
        coarse_wave_trials=int(args.coarse_wave_trials),
    )
    result = run_sweet_spot_search(cfg, progress_cb=lambda m: print(m))
    if result.best_focused_run is None:
        print("No se obtuvo ningun candidato valido en la fase focal.")
        return
    bundle = build_unified_report(args.db, result, output_dir=args.output_dir)
    print(_build_separator())
    print(f"Reporte unificado: {bundle['report_md']}")
    print(f"Carpeta del reporte: {bundle['report_dir']}")
    print(f"Mejor run_id: {bundle['best_run_id']}")
    print(f"Mejor seteo: {bundle['best_params']}")
    print(_build_separator())


def _cleanup(args: argparse.Namespace) -> None:
    aborted = abort_stale_runs(args.db)
    purged = purge_aborted_run_events(args.db) if args.purge_events else {"deleted_events": 0}
    print({"aborted_runs": aborted["aborted_runs"], **purged})


def _cache(args: argparse.Namespace) -> None:
    """Manage the optional Parquet cache for kline windows.

    Subactions:
      - materialize: build per-month Parquet files for a symbol/interval/window.
      - verify: report which monthly buckets exist and which are missing.

    Both honor `BACKTEST_PARQUET_CACHE` by checking pyarrow availability and
    falling back gracefully when missing.
    """
    from backtest.data_cache import (
        CACHE_ROOT_DEFAULT,
        _bucket_path,
        _month_buckets,
        is_available,
        materialize_window,
    )

    if not is_available():
        print({"status": "unavailable", "reason": "pyarrow not installed"})
        return

    if args.action == "materialize":
        paths = materialize_window(
            db_path=args.db,
            symbol=args.symbol,
            interval=args.interval,
            start_ts=int(args.start_ts),
            end_ts=int(args.end_ts),
            cache_root=args.cache_root,
            overwrite=bool(args.overwrite),
        )
        print({"status": "ok", "files": paths, "count": len(paths)})
        return

    if args.action == "verify":
        present, missing = [], []
        for year, month, _bs, _be in _month_buckets(int(args.start_ts), int(args.end_ts)):
            p = _bucket_path(args.cache_root, args.symbol, args.interval, year, month)
            (present if os.path.exists(p) else missing).append(p)
        print({"status": "ok", "present": present, "missing": missing})
        return

    print({"status": "error", "reason": f"unknown cache action: {args.action}"})


def _build_separator(width: int = 60) -> str:
    return "-" * width


def _menu(db_path: str) -> None:
    while True:
        print("\n=== Backtesting Terminal ===")
        print("1) Ejecutar backtest")
        print("2) Optimizar estrategia (Optuna)")
        print("3) Ver runs/trials")
        print("4) Graficar run")
        print("5) Buscar seteo dulce (sweet-spot + reporte)")
        print("6) Limpiar runs colgados")
        print("7) Salir")
        choice = input("Opción: ").strip()
        if choice == "1":
            symbol = input("Símbolo [BTCUSDT]: ").strip() or "BTCUSDT"
            interval = input("Intervalo [1h]: ").strip() or "1h"
            strategy = (input("Estrategia [dorothy|sma_cross] (def dorothy): ").strip() or "dorothy").lower()
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
                loop_seconds=60,
                fast=int((input("SMA fast [10]: ").strip() or "10")),
                slow=int((input("SMA slow [30]: ").strip() or "30")),
                profit_factor=float((input("Dorothy profit_factor [0.05]: ").strip() or "0.05")),
                margin_drop_factor=float((input("Dorothy margin_drop_factor [0.004]: ").strip() or "0.004")),
                quote_order_qty_usdt=float((input("Dorothy quote_order_qty_usdt [8]: ").strip() or "8")),
                max_rungs=int((input("Dorothy max_rungs [5]: ").strip() or "5")),
            )
            _run_once(args)
        elif choice == "2":
            symbol = input("Símbolo [BTCUSDT]: ").strip() or "BTCUSDT"
            interval = input("Intervalo [1h]: ").strip() or "1h"
            strategy = (input("Estrategia [dorothy|sma_cross] (def dorothy): ").strip() or "dorothy").lower()
            study = input("Study name [sma_opt]: ").strip() or "sma_opt"
            trials = int((input("Trials [30]: ").strip() or "30"))
            jobs = int((input(f"n_jobs CPU [{DEFAULT_N_JOBS}]: ").strip() or str(DEFAULT_N_JOBS)))
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
                timeout=None,
                quote_order_qty_usdt=8.0,
                max_rungs=5,
            )
            _optimize(args)
        elif choice == "3":
            _show(argparse.Namespace(db=db_path, run_id=None, limit=20, study=None, events_limit=25))
        elif choice == "4":
            run_id = int(input("run_id: ").strip())
            _plot(argparse.Namespace(db=db_path, run_id=run_id, study=None, output_dir="reports", signal_bins=30))
        elif choice == "5":
            symbol = input("Símbolo [BTCUSDT]: ").strip() or "BTCUSDT"
            interval = input("Intervalo [1h]: ").strip() or "1h"
            strategy = (input("Estrategia [dorothy|sma_cross] (def dorothy): ").strip() or "dorothy").lower()
            start_ts = input("start_ts (ms UTC, requerido): ").strip()
            end_ts = input("end_ts (ms UTC, requerido): ").strip()
            mode = (input("Modo recursos [safe|balanced|max-stable|adaptive_80] (def adaptive_80): ").strip() or "adaptive_80").lower()
            loop_seconds_raw = input("loop_seconds (vacio=desactivado): ").strip()
            env_guard = ResourceGuardConfig.from_env()
            sweet_args = argparse.Namespace(
                db=db_path,
                strategy=strategy,
                symbol=symbol,
                interval=interval,
                start_ts=start_ts,
                end_ts=end_ts,
                initial_cash=10000.0,
                fee_rate=0.001,
                slippage_bps=2.0,
                heikin_ashi=False,
                loop_seconds=int(loop_seconds_raw) if loop_seconds_raw else None,
                mode=mode,
                coarse_window_pct=0.25,
                coarse_trials=int((input("trials fase 1 [60]: ").strip() or "60")),
                top_k=int((input("top_k fase 2 [5]: ").strip() or "5")),
                objective_metric="total_return",
                direction="maximize",
                sampler="tpe",
                seed=42,
                guard_cpu_cap_pct=float(env_guard.cpu_cap_pct),
                guard_ram_cap_pct=float(env_guard.ram_cap_pct),
                guard_sample_sec=float(env_guard.sample_sec),
                guard_high_windows=int(env_guard.high_watermark_windows),
                guard_recover_windows=int(env_guard.recover_windows),
                guard_backoff_sec=10.0,
                coarse_wave_trials=12,
                output_dir="reports",
            )
            _sweet_spot(sweet_args)
        elif choice == "6":
            _cleanup(argparse.Namespace(db=db_path, purge_events=False))
        elif choice == "7":
            break
        else:
            print("Opción inválida.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Backtesting + optimization terminal")
    parser.add_argument("--db", default="klines.db")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run")
    p_run.add_argument("--strategy", default="dorothy", choices=("dorothy", "sma_cross"))
    p_run.add_argument("--symbol", required=True)
    p_run.add_argument("--interval", required=True)
    p_run.add_argument("--start_ts")
    p_run.add_argument("--end_ts")
    p_run.add_argument("--initial_cash", type=float, default=10000.0)
    p_run.add_argument("--fee_rate", type=float, default=0.001)
    p_run.add_argument("--slippage_bps", type=float, default=2.0)
    p_run.add_argument("--heikin_ashi", action="store_true")
    p_run.add_argument("--loop_seconds", type=int, help="Strategy execution loop in seconds")
    p_run.add_argument("--fast", type=int, default=10)
    p_run.add_argument("--slow", type=int, default=30)
    p_run.add_argument("--profit_factor", type=float, default=0.05)
    p_run.add_argument("--margin_drop_factor", type=float, default=0.004)
    p_run.add_argument("--quote_order_qty_usdt", type=float, default=8.0)
    p_run.add_argument("--max_rungs", type=int, default=5)

    p_opt = sub.add_parser("optimize")
    p_opt.add_argument("--strategy", default="dorothy", choices=("dorothy", "sma_cross"))
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
    p_opt.add_argument("--n_jobs", type=int, default=DEFAULT_N_JOBS)
    p_opt.add_argument("--timeout", type=int)
    p_opt.add_argument("--quote_order_qty_usdt", type=float, default=8.0)
    p_opt.add_argument("--max_rungs", type=int, default=5)

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

    p_sweet = sub.add_parser("sweet-spot", help="Buscar el seteo dulce en dos fases y producir reporte unificado")
    env_guard = ResourceGuardConfig.from_env()
    p_sweet.add_argument("--strategy", default="dorothy")
    p_sweet.add_argument("--symbol", required=True)
    p_sweet.add_argument("--interval", required=True)
    p_sweet.add_argument("--start_ts", required=True, help="Inicio del periodo (ms UTC)")
    p_sweet.add_argument("--end_ts", required=True, help="Fin del periodo (ms UTC)")
    p_sweet.add_argument("--initial_cash", type=float, default=10000.0)
    p_sweet.add_argument("--fee_rate", type=float, default=0.001)
    p_sweet.add_argument("--slippage_bps", type=float, default=2.0)
    p_sweet.add_argument("--heikin_ashi", action="store_true")
    p_sweet.add_argument("--loop_seconds", type=int)
    p_sweet.add_argument(
        "--mode",
        default="adaptive_80",
        choices=("safe", "balanced", "max-stable", "adaptive_80"),
    )
    p_sweet.add_argument("--coarse_window_pct", type=float, default=0.25)
    p_sweet.add_argument("--coarse_trials", type=int, default=60)
    p_sweet.add_argument("--top_k", type=int, default=5)
    p_sweet.add_argument("--objective_metric", default="total_return")
    p_sweet.add_argument("--direction", default="maximize", choices=("maximize", "minimize"))
    p_sweet.add_argument("--sampler", default="tpe", choices=("tpe", "random"))
    p_sweet.add_argument("--seed", type=int, default=42)
    p_sweet.add_argument("--guard_cpu_cap_pct", type=float, default=float(env_guard.cpu_cap_pct))
    p_sweet.add_argument("--guard_ram_cap_pct", type=float, default=float(env_guard.ram_cap_pct))
    p_sweet.add_argument("--guard_sample_sec", type=float, default=float(env_guard.sample_sec))
    p_sweet.add_argument("--guard_high_windows", type=int, default=int(env_guard.high_watermark_windows))
    p_sweet.add_argument("--guard_recover_windows", type=int, default=int(env_guard.recover_windows))
    p_sweet.add_argument("--guard_backoff_sec", type=float, default=10.0)
    p_sweet.add_argument("--coarse_wave_trials", type=int, default=12)
    p_sweet.add_argument("--output_dir", default="reports")

    p_clean = sub.add_parser("cleanup", help="Marcar runs colgados como aborted y purgar eventos")
    p_clean.add_argument("--purge_events", action="store_true")

    p_cache = sub.add_parser("cache", help="Gestionar cache columnar Parquet de klines")
    p_cache.add_argument("action", choices=("materialize", "verify"))
    p_cache.add_argument("--symbol", required=True)
    p_cache.add_argument("--interval", required=True)
    p_cache.add_argument("--start_ts", type=int, required=True, help="Inicio del periodo (ms UTC)")
    p_cache.add_argument("--end_ts", type=int, required=True, help="Fin del periodo (ms UTC)")
    p_cache.add_argument("--cache_root", default="reports/cache/parquet")
    p_cache.add_argument("--overwrite", action="store_true")

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
    elif args.cmd == "sweet-spot":
        _sweet_spot(args)
    elif args.cmd == "cleanup":
        _cleanup(args)
    elif args.cmd == "cache":
        _cache(args)
    else:
        _menu(args.db)


if __name__ == "__main__":
    main()

