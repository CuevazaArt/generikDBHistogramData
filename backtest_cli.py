"""Simple terminal interface for backtesting and optimization."""
import argparse
import datetime as dt
import json
from typing import Optional

from backtest.engine import EngineConfig
from backtest.optimize import optimize_strategy
from backtest.plots import export_study_summary_table, export_summary, plot_equity_and_drawdown, plot_signal_histograms, plot_trials
from backtest.registry import get_strategy, params_from_cli
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
    study = optimize_strategy(
        db_path=args.db,
        study_name=args.study,
        strategy_cls=strategy_cls,
        base_config=cfg,
        trials=args.trials,
        n_jobs=args.n_jobs,
        timeout=args.timeout,
    )
    print(f"Optimización completa. best_value={study.best_value:.6f}")
    print(f"best_params={study.best_params}")


def _show(args: argparse.Namespace) -> None:
    if args.run_id is not None:
        summary = summarize_run(args.db, run_id=args.run_id, events_limit=args.events_limit)
        desc = run_descriptor(args.db, run_id=args.run_id) or {}
        if desc:
            desc["first_event_iso_utc"] = _ms_to_iso(desc.get("first_event_time"))
            desc["last_event_iso_utc"] = _ms_to_iso(desc.get("last_event_time"))
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
        print("Gráficas y archivos exportados:")
        print({**paths, **signal_paths, **export})
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
        print("Resumen final de estudio:")
        print(summary_paths)
        if p:
            print(f"Gráfica de trials: {p}")


def _menu(db_path: str) -> None:
    while True:
        print("\n=== Backtesting Terminal ===")
        print("1) Ejecutar backtest")
        print("2) Optimizar estrategia (Optuna)")
        print("3) Ver runs/trials")
        print("4) Graficar run")
        print("5) Salir")
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
                fast=int((input("SMA fast [10]: ").strip() or "10")),
                slow=int((input("SMA slow [30]: ").strip() or "30")),
                profit_factor=float((input("Dorothy profit_factor [0.05]: ").strip() or "0.05")),
                margin_drop_factor=float((input("Dorothy margin_drop_factor [0.004]: ").strip() or "0.004")),
                quote_order_qty_usdt=float((input("Dorothy quote_order_qty_usdt [8]: ").strip() or "8")),
                min_order_notional=float((input("Dorothy min_order_notional [6]: ").strip() or "6")),
                max_order_notional=float((input("Dorothy max_order_notional [10]: ").strip() or "10")),
                max_active_orders=int((input("Dorothy max_active_orders [200]: ").strip() or "200")),
            )
            _run_once(args)
        elif choice == "2":
            symbol = input("Símbolo [BTCUSDT]: ").strip() or "BTCUSDT"
            interval = input("Intervalo [1h]: ").strip() or "1h"
            strategy = (input("Estrategia [dorothy|sma_cross] (def dorothy): ").strip() or "dorothy").lower()
            study = input("Study name [sma_opt]: ").strip() or "sma_opt"
            trials = int((input("Trials [30]: ").strip() or "30"))
            jobs = int((input("n_jobs CPU [2]: ").strip() or "2"))
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
                min_order_notional=6.0,
                max_order_notional=10.0,
                max_active_orders=200,
            )
            _optimize(args)
        elif choice == "3":
            _show(argparse.Namespace(db=db_path, run_id=None, limit=20, study=None, events_limit=25))
        elif choice == "4":
            run_id = int(input("run_id: ").strip())
            _plot(argparse.Namespace(db=db_path, run_id=run_id, study=None, output_dir="reports"))
        elif choice == "5":
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
    p_run.add_argument("--fast", type=int, default=10)
    p_run.add_argument("--slow", type=int, default=30)
    p_run.add_argument("--profit_factor", type=float, default=0.05)
    p_run.add_argument("--margin_drop_factor", type=float, default=0.004)
    p_run.add_argument("--quote_order_qty_usdt", type=float, default=8.0)
    p_run.add_argument("--min_order_notional", type=float, default=6.0)
    p_run.add_argument("--max_order_notional", type=float, default=10.0)
    p_run.add_argument("--max_active_orders", type=int, default=200)

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
    p_opt.add_argument("--n_jobs", type=int, default=2)
    p_opt.add_argument("--timeout", type=int)
    p_opt.add_argument("--quote_order_qty_usdt", type=float, default=8.0)
    p_opt.add_argument("--min_order_notional", type=float, default=6.0)
    p_opt.add_argument("--max_order_notional", type=float, default=10.0)
    p_opt.add_argument("--max_active_orders", type=int, default=200)

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

