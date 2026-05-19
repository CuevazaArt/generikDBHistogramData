"""Plot and export helpers for backtest analysis."""
import csv
import json
import os
from typing import Dict, List, Tuple

try:
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover - runtime guard
    plt = None


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def plot_equity_and_drawdown(equity_rows: List[Tuple], output_dir: str, run_id: int) -> Dict[str, str]:
    if plt is None:
        raise RuntimeError("Matplotlib is not installed. Run: pip install -r requirements.txt")
    ensure_dir(output_dir)
    seq = [int(r[0]) for r in equity_rows]
    equity = [float(r[2]) for r in equity_rows]
    if not seq or not equity:
        return {}
    peak = equity[0]
    drawdown = []
    for e in equity:
        peak = max(peak, e)
        dd = (peak - e) / peak if peak > 0 else 0.0
        drawdown.append(dd)

    equity_path = os.path.join(output_dir, f"run_{run_id}_equity.png")
    dd_path = os.path.join(output_dir, f"run_{run_id}_drawdown.png")
    ret_hist_path = os.path.join(output_dir, f"run_{run_id}_returns_hist.png")

    plt.figure(figsize=(10, 4))
    plt.plot(seq, equity)
    plt.title(f"Equity curve - run {run_id}")
    plt.xlabel("step")
    plt.ylabel("equity")
    plt.tight_layout()
    plt.savefig(equity_path)
    plt.close()

    plt.figure(figsize=(10, 4))
    plt.plot(seq, drawdown)
    plt.title(f"Drawdown - run {run_id}")
    plt.xlabel("step")
    plt.ylabel("drawdown")
    plt.tight_layout()
    plt.savefig(dd_path)
    plt.close()

    returns = []
    for i in range(1, len(equity)):
        if equity[i - 1] > 0:
            returns.append((equity[i] - equity[i - 1]) / equity[i - 1])
    if returns:
        plt.figure(figsize=(8, 4))
        plt.hist(returns, bins=30)
        plt.title(f"Returns distribution - run {run_id}")
        plt.tight_layout()
        plt.savefig(ret_hist_path)
        plt.close()

    return {"equity": equity_path, "drawdown": dd_path, "returns_hist": ret_hist_path}


def plot_trials(trial_rows: List[Tuple], output_dir: str, study_name: str) -> str:
    if plt is None:
        raise RuntimeError("Matplotlib is not installed. Run: pip install -r requirements.txt")
    ensure_dir(output_dir)
    xs = [int(r[0]) for r in trial_rows]
    ys = [float(r[1]) for r in trial_rows]
    if not xs:
        return ""
    out = os.path.join(output_dir, f"study_{study_name}_trials.png")
    plt.figure(figsize=(10, 4))
    plt.plot(xs, ys, marker="o", linestyle="-")
    plt.title(f"Objective by trial - {study_name}")
    plt.xlabel("trial_number")
    plt.ylabel("objective")
    plt.tight_layout()
    plt.savefig(out)
    plt.close()
    return out


def export_summary(output_dir: str, file_stem: str, metrics: Dict[str, float], equity_rows: List[Tuple]) -> Dict[str, str]:
    ensure_dir(output_dir)
    json_path = os.path.join(output_dir, f"{file_stem}_metrics.json")
    csv_path = os.path.join(output_dir, f"{file_stem}_equity.csv")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["seq", "event_time", "equity"])
        for r in equity_rows:
            writer.writerow(r)
    return {"metrics_json": json_path, "equity_csv": csv_path}

