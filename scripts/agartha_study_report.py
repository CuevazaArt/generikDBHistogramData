"""Report top trials of an Agartha Optuna study.

Lee el SQLite del backtest, extrae los trials del study con sus parametros y
metricas finales, ordena por total_return (default) y genera:

  - SWEET_SPOTS.md (top N seteos con metricas comparables)
  - param_scatter.png (3 subplots: trailing/activation/breakeven vs return)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List

try:
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover
    plt = None

from backtest.report_paths import study_report_dir


def _load_trials(db_path: str, study_name: str) -> List[Dict[str, Any]]:
    c = sqlite3.connect(db_path)
    try:
        rows = c.execute(
            """
            SELECT trial_id, objective, params_json
            FROM bt_trials
            WHERE study_name = ?
            ORDER BY objective DESC NULLS LAST
            """,
            (study_name,),
        ).fetchall()
        # Cargamos metricas en formato (trial_id, name, value) -> dict por trial
        metrics_rows = c.execute(
            """
            SELECT m.trial_id, m.metric_name, m.metric_value
            FROM bt_trial_metrics m
            JOIN bt_trials t ON t.trial_id = m.trial_id
            WHERE t.study_name = ?
            """,
            (study_name,),
        ).fetchall()
    finally:
        c.close()
    metrics_by_trial: Dict[int, Dict[str, float]] = {}
    for tid, name, value in metrics_rows:
        metrics_by_trial.setdefault(int(tid), {})[str(name)] = float(value)
    out: List[Dict[str, Any]] = []
    for trial_id, objective, params_json in rows:
        if objective is None:
            continue
        out.append({
            "trial_id": int(trial_id),
            "objective": float(objective),
            "params": json.loads(params_json or "{}"),
            "metrics": metrics_by_trial.get(int(trial_id), {}),
        })
    return out


def _write_sweet_md(path: str, trials: List[Dict[str, Any]], top_n: int, study_name: str) -> None:
    top = trials[:top_n]
    lines = [
        f"# Sweet spots - {study_name}",
        "",
        f"Top {len(top)} de {len(trials)} trials, ordenados por `total_return` (objective).",
        "",
        "## Mejor seteo",
        "",
    ]
    if top:
        best = top[0]
        m = best["metrics"]
        p = best["params"]
        lines += [
            f"- **trial:** `{best['trial_id']}`",
            f"- **trailing_stop_pct:** `{p.get('trailing_stop_pct'):.3f}`" if p.get("trailing_stop_pct") is not None else "",
            f"- **activation_profit_pct:** `{p.get('activation_profit_pct'):.3f}`" if p.get("activation_profit_pct") is not None else "",
            f"- **breakeven_lock_pct:** `{p.get('breakeven_lock_pct'):.3f}`" if p.get("breakeven_lock_pct") is not None else "",
            f"- **total_return:** `{best['objective']*100:.2f}%`",
            f"- **final_equity:** `{m.get('final_equity', 0):.4f} USDT`",
            f"- **max_drawdown:** `{m.get('max_drawdown', 0)*100:.2f}%`",
            f"- **num_trades:** `{int(m.get('num_trades', 0))}`",
            f"- **win_rate:** `{m.get('win_rate', 0)*100:.1f}%`",
            f"- **sharpe:** `{m.get('sharpe', 0):.3f}`",
            "",
        ]
    lines.append("## Tabla top trials")
    lines.append("")
    lines.append("| trial | trailing% | activation% | breakeven% | return% | final_eq | DD% | trades | win% |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for t in top:
        p = t["params"]
        m = t["metrics"]
        lines.append(
            f"| {t['trial_id']} "
            f"| {p.get('trailing_stop_pct', 0):.2f} "
            f"| {p.get('activation_profit_pct', 0):.2f} "
            f"| {p.get('breakeven_lock_pct', 0):.2f} "
            f"| {t['objective']*100:+.2f} "
            f"| {m.get('final_equity', 0):.3f} "
            f"| {m.get('max_drawdown', 0)*100:.2f} "
            f"| {int(m.get('num_trades', 0))} "
            f"| {m.get('win_rate', 0)*100:.0f} |"
        )
    lines.append("")
    lines.append("## Estadisticas globales del estudio")
    lines.append("")
    if trials:
        objs = [t["objective"] for t in trials]
        avg = sum(objs) / len(objs)
        n_pos = sum(1 for o in objs if o > 0)
        lines += [
            f"- trials: **{len(trials)}**",
            f"- return medio: **{avg*100:+.2f}%**",
            f"- trials positivos: **{n_pos}** ({n_pos/len(trials)*100:.0f}%)",
            f"- mejor: **{objs[0]*100:+.2f}%**",
            f"- peor: **{objs[-1]*100:+.2f}%**",
        ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join([l for l in lines if l is not None]))


def _plot_scatters(path: str, trials: List[Dict[str, Any]], study_name: str) -> None:
    if plt is None or not trials:
        return
    params_names = ("trailing_stop_pct", "activation_profit_pct", "breakeven_lock_pct")
    fig, axes = plt.subplots(1, 3, figsize=(15, 5))
    objs = [t["objective"] * 100 for t in trials]
    for ax, pname in zip(axes, params_names):
        xs = [t["params"].get(pname, 0.0) for t in trials]
        sc = ax.scatter(xs, objs, c=objs, cmap="RdYlGn", s=40, edgecolor="black", linewidth=0.3)
        # Marcar el best
        best = max(trials, key=lambda t: t["objective"])
        ax.scatter([best["params"].get(pname, 0.0)], [best["objective"] * 100],
                   marker="*", s=200, color="gold", edgecolor="black", linewidth=1, zorder=5,
                   label=f"best ({best['objective']*100:+.1f}%)")
        ax.axhline(0, color="grey", linewidth=0.5, linestyle="--")
        ax.set_xlabel(pname)
        ax.set_ylabel("total_return (%)")
        ax.grid(True, alpha=0.25)
        ax.legend(loc="lower right", fontsize=8)
    fig.suptitle(f"Agartha sweet-spot search - {study_name}")
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sweet spots report for an Agartha Optuna study.")
    parser.add_argument("--db", default="klines.db")
    parser.add_argument("--study", required=True)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--output_root", default="reports")
    args = parser.parse_args()

    trials = _load_trials(args.db, args.study)
    if not trials:
        raise SystemExit(f"No trials found for study '{args.study}' in {args.db}")
    out_dir = study_report_dir(args.output_root, args.study)
    md_path = os.path.join(out_dir, "SWEET_SPOTS.md")
    png_path = os.path.join(out_dir, "param_scatter.png")
    _write_sweet_md(md_path, trials, top_n=int(args.top), study_name=args.study)
    _plot_scatters(png_path, trials, study_name=args.study)
    print(f"[study-report] trials: {len(trials)} | top {args.top}")
    print(f"[study-report] best: total_return={trials[0]['objective']*100:+.2f}% "
          f"params={trials[0]['params']}")
    print(f"[study-report] SWEET_SPOTS.md: {md_path}")
    print(f"[study-report] scatter: {png_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
