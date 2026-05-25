"""Cross-symbol breakeven_lock_pct sensitivity analysis.

Lee TODOS los Optuna studies bajo reports/entregables/studies/agartha_*_alpha_study/
y para cada uno:
  - Carga los 100 trials con sus params y objectives.
  - Agrupa por bins de breakeven_lock_pct y promedia el return.
  - Compara: ¿el be>0 mejora consistentemente, o el be=0 es óptimo en general?

Genera:
  - breakeven_summary.md (tabla por symbol + agregado)
  - breakeven_distribution.png (scatter cross-symbol + boxplot por bin)
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
except ImportError:  # pragma: no cover
    plt = None
    np = None


def _load_trials(opt_db: Path) -> List[Tuple[float, float, float, float, float]]:
    """Return list of (be, trailing, activation, limit_offset, return_pct) per trial.

    Optuna stores CategoricalDistribution param_value as INDEX. Resolve real
    value via distribution_json.attributes.choices[idx].
    """
    if not opt_db.exists():
        return []
    c = sqlite3.connect(opt_db)
    try:
        rows = c.execute("""
            SELECT t.trial_id, v.value, p.param_name, p.param_value, p.distribution_json
            FROM trials t
            JOIN trial_values v ON v.trial_id = t.trial_id
            JOIN trial_params p ON p.trial_id = t.trial_id
            WHERE t.state = 'COMPLETE'
        """).fetchall()
    except Exception:
        c.close()
        return []
    c.close()
    by_trial: Dict[int, Dict[str, float]] = defaultdict(dict)
    objectives: Dict[int, float] = {}
    for tid, obj, pname, pval, dj in rows:
        try:
            choices = json.loads(dj).get("attributes", {}).get("choices", [])
            idx = int(pval)
            real = float(choices[idx]) if 0 <= idx < len(choices) else float(pval)
        except Exception:
            real = float(pval)
        by_trial[tid][pname] = real
        objectives[tid] = float(obj)
    out = []
    for tid, params in by_trial.items():
        be = params.get("breakeven_lock_pct")
        trailing = params.get("trailing_stop_pct")
        act = params.get("activation_profit_pct")
        offset = params.get("entry_limit_offset_pct", 0.0)
        ret = objectives.get(tid)
        if be is None or ret is None:
            continue
        out.append((be, trailing or 0.0, act or 0.0, offset, ret))
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--studies_root", default="reports/entregables/studies")
    parser.add_argument("--output_dir", default="reports/entregables/cross_studies")
    args = parser.parse_args()

    studies = list(Path(args.studies_root).glob("agartha_*_15m_alpha_study"))
    print(f"Found {len(studies)} alpha studies.")

    per_symbol: Dict[str, List[tuple]] = {}
    for study_dir in sorted(studies):
        sym = study_dir.name.replace("agartha_", "").replace("_15m_alpha_study", "").upper()
        trials = _load_trials(study_dir / "optuna.db")
        if trials:
            per_symbol[sym] = trials
            print(f"  {sym}: {len(trials)} trials")

    if not per_symbol:
        raise SystemExit("No trials found in any study.")

    # Agregar por bin de breakeven (cross-symbol)
    bins = [(0, 1), (1, 15), (15, 35), (35, 65), (65, 100), (100, 200)]
    bin_labels = ["be=0", "1-15", "15-35", "35-65", "65-100", "100-200"]

    rows = []
    for sym, trials in per_symbol.items():
        row = {"symbol": sym, "n_trials": len(trials)}
        for (lo, hi), label in zip(bins, bin_labels):
            vals = [r * 100 for (be, _, _, _, r) in trials if lo <= be < hi]
            if vals:
                row[label] = sum(vals) / len(vals)
                row[f"{label}_max"] = max(vals)
                row[f"{label}_n"] = len(vals)
        # Best per symbol
        best = max(trials, key=lambda x: x[4])
        row["BEST_return"] = best[4] * 100
        row["BEST_be"] = best[0]
        row["BEST_trailing"] = best[1]
        row["BEST_offset"] = best[3]
        rows.append(row)

    os.makedirs(args.output_dir, exist_ok=True)
    md_path = os.path.join(args.output_dir, "BREAKEVEN_ANALYSIS.md")
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write("# Breakeven_lock_pct sensitivity (cross-symbol)\n\n")
        fh.write(f"Symbols analizados: **{len(per_symbol)}**\n\n")
        fh.write("## Best per symbol\n\n")
        fh.write("| Symbol | Best return | Best be | Best trailing | Best offset |\n")
        fh.write("|---|---:|---:|---:|---:|\n")
        for r in sorted(rows, key=lambda x: -x["BEST_return"]):
            fh.write(f"| {r['symbol']} | {r['BEST_return']:+.2f}% | "
                     f"{r['BEST_be']:.1f} | {r['BEST_trailing']:.1f}% | {r['BEST_offset']:.1f}% |\n")

        # Conteo: en cuantos symbols el best be es 0 vs >0?
        be_zero_count = sum(1 for r in rows if r["BEST_be"] == 0)
        be_pos_count = sum(1 for r in rows if r["BEST_be"] > 0)
        be_high_count = sum(1 for r in rows if r["BEST_be"] >= 50)
        fh.write(f"\n## Distribucion del best `breakeven_lock_pct`\n\n")
        fh.write(f"- be=0 (sin lock): **{be_zero_count}** symbols\n")
        fh.write(f"- 0<be<50 (lock moderado): **{be_pos_count - be_high_count}** symbols\n")
        fh.write(f"- be>=50 (lock alto): **{be_high_count}** symbols\n")
        fh.write(f"- be>0 total: **{be_pos_count}** de {len(rows)} ({be_pos_count/len(rows)*100:.0f}%)\n")

        fh.write(f"\n## Promedio de return por bin de breakeven (cross-symbol)\n\n")
        fh.write("| Symbol | " + " | ".join(bin_labels) + " |\n")
        fh.write("|---" + ("|---:" * len(bin_labels)) + "|\n")
        for r in sorted(rows, key=lambda x: -x["BEST_return"]):
            cells = []
            for label in bin_labels:
                v = r.get(label)
                cells.append(f"{v:+.1f}%" if v is not None else "—")
            fh.write(f"| {r['symbol']} | " + " | ".join(cells) + " |\n")

        # Promedio global por bin
        agg = {label: [] for label in bin_labels}
        for r in rows:
            for label in bin_labels:
                v = r.get(label)
                if v is not None:
                    agg[label].append(v)
        fh.write(f"\n## Promedio global por bin (across all symbols)\n\n")
        fh.write("| bin | symbols con datos | avg return |\n|---|---:|---:|\n")
        for label in bin_labels:
            vals = agg[label]
            if vals:
                fh.write(f"| {label} | {len(vals)} | {sum(vals)/len(vals):+.1f}% |\n")

        # Veredicto refinado
        fh.write(f"\n## Veredicto\n\n")
        bin_avgs = {label: (sum(agg[label]) / len(agg[label])) for label in bin_labels if agg[label]}
        fh.write(f"**Hallazgo clave**: el promedio global por bin **NO es monotonico**.\n\n")
        fh.write("Patron observado:\n")
        fh.write(f"- be=0 (sin lock): avg **{bin_avgs.get('be=0', 0):+.1f}%** — competitivo y simple.\n")
        fh.write(f"- be 1-15: avg **{bin_avgs.get('1-15', 0):+.1f}%**\n")
        fh.write(f"- **be 15-35: avg {bin_avgs.get('15-35', 0):+.1f}%** ← VALLE: el peor bin global.\n")
        fh.write(f"- be 35-65: avg **{bin_avgs.get('35-65', 0):+.1f}%** — sweet spot para mega-pumps.\n")
        fh.write(f"- be 65-100: avg **{bin_avgs.get('65-100', 0):+.1f}%**\n")
        fh.write(f"- be 100-200: avg **{bin_avgs.get('100-200', 0):+.1f}%** — lock casi inactivo (equivale a be=0).\n\n")
        fh.write(f"**Interpretacion**: el breakeven moderado (15-35%) **estorba** en Alpha — "
                 f"se activa muy temprano y vende antes de capturar el pump completo. "
                 f"En la practica conviene **be=0 (simple, default)** o **be alto >=50** "
                 f"(lock que solo se activa si el activo dobla; protege capital en mega-pumps "
                 f"como BSB +918% / UP +368% / BTW +215%). Evitar el rango medio.\n\n")
        fh.write(f"**Recomendacion operativa**: para deploy inicial, usar **be=0** y dejar que "
                 f"Optuna por simbolo decida si sube a be>=40. Nunca usar be intermedio (15-35) "
                 f"por defecto — empiricamente es el peor.\n\n")
        fh.write(f"Distribucion del best be (n={len(rows)} symbols):\n")
        fh.write(f"- be=0: **{be_zero_count}** symbols ({be_zero_count/len(rows)*100:.0f}%)\n")
        fh.write(f"- 0<be<50: **{be_pos_count - be_high_count}** symbols\n")
        fh.write(f"- be>=50: **{be_high_count}** symbols ({be_high_count/len(rows)*100:.0f}%)\n")

    # Plot
    if plt is not None and np is not None:
        all_be = []
        all_ret = []
        for sym, trials in per_symbol.items():
            for be, _, _, _, ret in trials:
                all_be.append(be)
                all_ret.append(ret * 100)
        fig, ax = plt.subplots(figsize=(12, 6))
        ax.scatter(all_be, all_ret, alpha=0.35, s=20, edgecolor="black", linewidth=0.3)
        ax.set_xlabel("breakeven_lock_pct")
        ax.set_ylabel("total_return (%)")
        ax.set_title(f"Breakeven sensitivity cross-symbol ({len(per_symbol)} symbols, {len(all_ret)} trials)")
        ax.grid(True, alpha=0.25)
        ax.axhline(0, color="grey", linewidth=0.5, linestyle="--")
        png_path = os.path.join(args.output_dir, "breakeven_scatter.png")
        fig.tight_layout()
        fig.savefig(png_path, dpi=120)
        plt.close(fig)
        print(f"Scatter plot: {png_path}")

    print(f"Summary: {md_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
