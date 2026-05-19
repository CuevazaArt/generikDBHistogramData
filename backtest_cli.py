"""Simple terminal interface for backtesting and optimization."""
import argparse
import json
from typing import Optional

from backtest.engine import EngineConfig
from backtest.optimize import optimize_strategy
from backtest.plots import export_summary, plot_equity_and_drawdown, plot_trials
from backtest.runner import execute_and_persist
from backtest.storage import (
    list_runs,
    run_equity_curve,
    summarize_run,
    top_trials,
    trial_objectives,
)
from backtest.strategies import SmaCrossStrategy
from db import init_db


def _parse_ts(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    return int(v)


def _run_once(args: argparse.Namespace) -> None:
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
        sma_fast=args.fast,
        sma_slow=args.slow,
    )
    result = execute_and_persist(
        config=cfg,
        strategy_cls=SmaCrossStrategy,
        strategy_params={"fast": args.fast, "slow": args.slow},
    )
    print(f"Run terminado. run_id={result.run_id}")
    print(json.dumps(result.metrics, ensure_ascii=False, indent=2))


def _optimize(args: argparse.Namespace) -> None:
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
        strategy_cls=SmaCrossStrategy,
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
        print(f"Resumen run_id={args.run_id}")
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
        metrics = summarize_run(args.db, run_id=args.run_id)["metrics"]
        export = export_summary(args.output_dir, f"run_{args.run_id}", metrics, eq_rows)
        print("Gráficas y archivos exportados:")
        print({**paths, **export})
    if args.study:
        p = plot_trials(
            trial_rows=trial_objectives(args.db, study_name=args.study, limit=1000),
            output_dir=args.output_dir,
            study_name=args.study,
        )
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
            fast = int((input("SMA fast [10]: ").strip() or "10"))
            slow = int((input("SMA slow [30]: ").strip() or "30"))
            args = argparse.Namespace(
                db=db_path,
                symbol=symbol,
                interval=interval,
                start_ts=None,
                end_ts=None,
                initial_cash=10000.0,
                fee_rate=0.001,
                slippage_bps=2.0,
                heikin_ashi=False,
                fast=fast,
                slow=slow,
            )
            _run_once(args)
        elif choice == "2":
            symbol = input("Símbolo [BTCUSDT]: ").strip() or "BTCUSDT"
            interval = input("Intervalo [1h]: ").strip() or "1h"
            study = input("Study name [sma_opt]: ").strip() or "sma_opt"
            trials = int((input("Trials [30]: ").strip() or "30"))
            jobs = int((input("n_jobs CPU [2]: ").strip() or "2"))
            args = argparse.Namespace(
                db=db_path,
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

    p_opt = sub.add_parser("optimize")
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

    p_show = sub.add_parser("show")
    p_show.add_argument("--run_id", type=int)
    p_show.add_argument("--limit", type=int, default=20)
    p_show.add_argument("--study")
    p_show.add_argument("--events_limit", type=int, default=25)

    p_plot = sub.add_parser("plot")
    p_plot.add_argument("--run_id", type=int)
    p_plot.add_argument("--study")
    p_plot.add_argument("--output_dir", default="reports")

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

