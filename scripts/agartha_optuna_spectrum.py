"""Agartha Optuna study with discretized + extreme params, and spectrum plot.

Diferencias frente a la corrida anterior:

  - **Saltos grandes** entre seteos via `suggest_categorical` sobre listas
    discretas (no continuo).
  - **N_extreme** trials con valores ridiculos (trailing 1%, 80%, 90%;
    activation 100-200%; breakeven 50-100%) forzados con `enqueue_trial`.
  - Cada trial persiste su run en `bt_runs` (via `execute_and_persist`);
    guardamos el mapping trial_number -> run_id en JSON para post-analisis.
  - Al final genera **spectrum.png**: overlay de las N equity y DD curves
    en un solo grafico (alpha bajo + colormap por return).

Uso:
  python scripts/agartha_optuna_spectrum.py \
      --study agartha_bill_15m_wide --trials 100 --extreme 20 \
      --initial_cash 10 --quote_order_qty_usdt 10 \
      --start_ts 1777878000000 --end_ts 1779516000000
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import optuna

from backtest.engine import EngineConfig
from backtest.registry import get_strategy
from backtest.report_paths import study_report_dir
from backtest.runner import execute_and_persist
from backtest.storage import run_equity_curve, summarize_run

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import cm
    from matplotlib.colors import Normalize
except ImportError:  # pragma: no cover
    plt = None


# Discretizacion principal (saltos finos) -- 18 x 9 x 7 = 1134 combinaciones posibles
NORMAL_TRAILING = [
    8.0, 10.0, 12.0, 15.0, 18.0, 20.0, 22.0, 25.0, 28.0,
    30.0, 33.0, 35.0, 38.0, 40.0, 45.0, 50.0, 55.0, 60.0,
]
NORMAL_ACTIVATION = [0.0, 5.0, 10.0, 20.0, 30.0, 40.0, 50.0, 65.0, 80.0]
NORMAL_BREAKEVEN = [0.0, 5.0, 10.0, 20.0, 30.0, 40.0, 60.0]

# Combinaciones ridiculas (extremos)
EXTREME_TRAILING = [0.5, 1.0, 2.0, 3.0, 5.0, 70.0, 75.0, 80.0, 85.0, 90.0, 95.0]
EXTREME_ACTIVATION = [0.0, 90.0, 100.0, 120.0, 150.0, 180.0, 200.0, 250.0]
EXTREME_BREAKEVEN = [0.0, 70.0, 75.0, 85.0, 100.0, 125.0, 150.0]


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_extreme_combos(n: int, seed: int = 42) -> List[Dict[str, float]]:
    """N combinaciones unicas de extremos (cartesian product subsampled)."""
    rng = random.Random(seed)
    full = [
        {"trailing_stop_pct": t, "activation_profit_pct": a, "breakeven_lock_pct": b}
        for t in EXTREME_TRAILING for a in EXTREME_ACTIVATION for b in EXTREME_BREAKEVEN
    ]
    rng.shuffle(full)
    return full[:n]


def _build_objective(
    *,
    db_path: str,
    symbol: str,
    interval: str,
    start_ts: int,
    end_ts: int,
    initial_cash: float,
    fee_rate: float,
    slippage_bps: float,
    quote_order_qty_usdt: float,
    max_cycles: int,
    trial_to_run: Dict[int, int],
):
    strategy_cls = get_strategy("agartha")

    def objective(trial: optuna.Trial) -> float:
        trailing = float(trial.suggest_categorical("trailing_stop_pct",
                                                   NORMAL_TRAILING + EXTREME_TRAILING))
        activation = float(trial.suggest_categorical("activation_profit_pct",
                                                     NORMAL_ACTIVATION + EXTREME_ACTIVATION))
        breakeven = float(trial.suggest_categorical("breakeven_lock_pct",
                                                    NORMAL_BREAKEVEN + EXTREME_BREAKEVEN))
        params = {
            "quote_order_qty_usdt": float(quote_order_qty_usdt),
            "trailing_stop_pct": trailing,
            "activation_profit_pct": activation,
            "breakeven_lock_pct": breakeven,
            "max_holding_bars": 0,
            "partial_tp_pct": 0.0,
            "partial_tp_size_pct": 0.0,
            "max_cycles": int(max_cycles),
            "reentry_cooldown_bars": 0,
        }
        cfg = EngineConfig(
            db_path=db_path,
            symbol=symbol.upper(),
            interval=interval,
            start_ts=int(start_ts),
            end_ts=int(end_ts),
            initial_cash=float(initial_cash),
            fee_rate=float(fee_rate),
            slippage_bps=float(slippage_bps),
            events_mode="lite",
            snapshot_seconds=3600,
        )
        result = execute_and_persist(
            config=cfg, strategy_cls=strategy_cls, strategy_params=params,
        )
        trial_to_run[trial.number] = int(result.run_id)
        return float(result.metrics.get("total_return", 0.0))

    return objective


def _spectrum_plot(
    *,
    db_path: str,
    trial_to_run: Dict[int, int],
    output_path: str,
    title: str,
) -> None:
    """Overlay de equity + DD por trial. Color codificado por total_return final."""
    if plt is None:
        return
    series: List[Tuple[int, List[int], List[float], List[float], float]] = []
    for trial_no, run_id in sorted(trial_to_run.items()):
        rows = run_equity_curve(db_path, run_id=int(run_id))
        if not rows:
            continue
        times = [int(r[1]) for r in rows]
        equity = [float(r[2]) for r in rows]
        peak = equity[0]
        dd = []
        for e in equity:
            peak = max(peak, e)
            dd.append((peak - e) / peak * 100.0 if peak > 0 else 0.0)
        final_eq = equity[-1]
        initial_eq = equity[0]
        ret = (final_eq - initial_eq) / initial_eq * 100.0 if initial_eq > 0 else 0.0
        series.append((trial_no, times, equity, dd, ret))

    if not series:
        return

    rets = [s[4] for s in series]
    vmin, vmax = min(rets), max(rets)
    norm = Normalize(vmin=vmin, vmax=vmax)
    cmap = cm.get_cmap("RdYlGn")

    fig, (ax_eq, ax_dd) = plt.subplots(2, 1, figsize=(13, 8), sharex=True)
    for trial_no, times, equity, dd, ret in series:
        color = cmap(norm(ret))
        ts = [datetime.fromtimestamp(t / 1000.0, tz=timezone.utc) for t in times]
        ax_eq.plot(ts, equity, color=color, alpha=0.35, linewidth=0.8)
        ax_dd.plot(ts, dd, color=color, alpha=0.30, linewidth=0.7)

    # Highlight top y bottom
    best = max(series, key=lambda s: s[4])
    worst = min(series, key=lambda s: s[4])
    for s, label_prefix, lw, alpha in [(best, "BEST", 2.0, 1.0), (worst, "WORST", 1.6, 0.9)]:
        ts = [datetime.fromtimestamp(t / 1000.0, tz=timezone.utc) for t in s[1]]
        ax_eq.plot(ts, s[2], color=cmap(norm(s[4])), linewidth=lw, alpha=alpha,
                   label=f"{label_prefix} trial {s[0]} ({s[4]:+.1f}%)")
        ax_dd.plot(ts, s[3], color=cmap(norm(s[4])), linewidth=lw, alpha=alpha)

    ax_eq.set_title(f"{title} - Equity spectrum ({len(series)} trials)")
    ax_eq.set_ylabel("Equity (USDT)")
    ax_eq.grid(True, alpha=0.25)
    ax_eq.legend(loc="upper left", fontsize=9)
    ax_dd.set_title("Drawdown spectrum")
    ax_dd.set_ylabel("Drawdown (%)")
    ax_dd.set_xlabel("Tiempo (UTC)")
    ax_dd.grid(True, alpha=0.25)
    ax_dd.set_ylim(bottom=0)
    fig.autofmt_xdate()

    # Colorbar
    sm = cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cbar = fig.colorbar(sm, ax=[ax_eq, ax_dd], orientation="vertical", pad=0.02, aspect=40)
    cbar.set_label("total_return (%)")

    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    fig.savefig(output_path, dpi=120)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Agartha optuna study with extremes + spectrum.")
    parser.add_argument("--db", default="klines.db")
    parser.add_argument("--symbol", default="BILLUSDT")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--start_ts", type=int, required=True)
    parser.add_argument("--end_ts", type=int, required=True)
    parser.add_argument("--initial_cash", type=float, default=10.0)
    parser.add_argument("--quote_order_qty_usdt", type=float, default=10.0)
    parser.add_argument("--fee_rate", type=float, default=0.001)
    parser.add_argument("--slippage_bps", type=float, default=10.0)
    parser.add_argument("--max_cycles", type=int, default=0)
    parser.add_argument("--study", required=True)
    parser.add_argument("--trials", type=int, default=100)
    parser.add_argument("--extreme", type=int, default=20)
    parser.add_argument("--output_root", default="reports")
    args = parser.parse_args()

    if args.extreme >= args.trials:
        raise SystemExit("--extreme debe ser < --trials")

    out_dir = study_report_dir(args.output_root, args.study)
    trial_to_run: Dict[int, int] = {}
    objective = _build_objective(
        db_path=args.db, symbol=args.symbol, interval=args.interval,
        start_ts=args.start_ts, end_ts=args.end_ts,
        initial_cash=args.initial_cash, fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps, quote_order_qty_usdt=args.quote_order_qty_usdt,
        max_cycles=args.max_cycles, trial_to_run=trial_to_run,
    )

    # Optuna en SQLite separado para no contender con bt_runs.
    storage_path = os.path.join(out_dir, "optuna.db")
    storage_url = f"sqlite:///{storage_path}"
    study = optuna.create_study(
        study_name=args.study,
        storage=storage_url,
        load_if_exists=True,
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    extreme_combos = _generate_extreme_combos(int(args.extreme))
    for combo in extreme_combos:
        # Enqueue forzados (los TPE no eligen estos, los enqueueamos).
        study.enqueue_trial(combo)
    print(f"[optuna-spectrum] enqueued {len(extreme_combos)} extreme trials")

    study.optimize(objective, n_trials=int(args.trials), n_jobs=1, show_progress_bar=False)
    print(f"[optuna-spectrum] done. best={study.best_value:.4f} params={study.best_params}")

    mapping_path = os.path.join(out_dir, "trial_to_run.json")
    with open(mapping_path, "w", encoding="utf-8") as fh:
        json.dump({
            "study_name": args.study,
            "trials": {str(k): v for k, v in trial_to_run.items()},
            "best_trial_number": study.best_trial.number,
            "best_value": float(study.best_value),
            "best_params": study.best_params,
            "extreme_combos": extreme_combos,
            "generated_at": _utc_iso(),
        }, fh, ensure_ascii=False, indent=2)

    spectrum_path = os.path.join(out_dir, "spectrum.png")
    _spectrum_plot(
        db_path=args.db, trial_to_run=trial_to_run,
        output_path=spectrum_path,
        title=f"{args.study} ({args.symbol}/{args.interval}, cash={args.initial_cash})",
    )

    print(f"[optuna-spectrum] mapping: {mapping_path}")
    print(f"[optuna-spectrum] spectrum: {spectrum_path}")
    print(f"[optuna-spectrum] out_dir: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
