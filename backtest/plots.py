"""Plot and export helpers for backtest analysis."""
import csv
import json
import statistics
import os
from typing import Dict, List, Tuple

try:
    import matplotlib.pyplot as plt  # type: ignore[import-not-found]
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


def plot_signal_histograms(signal_rows: List[Tuple], output_dir: str, run_id: int, bins: int = 30) -> Dict[str, str]:
    if plt is None:
        raise RuntimeError("Matplotlib is not installed. Run: pip install -r requirements.txt")
    ensure_dir(output_dir)
    if not signal_rows:
        return {}

    # 1) Executed operations only (fills): entry/exit moments.
    fill_buy_seq = [int(r[0]) for r in signal_rows if r[2] == "fill" and r[3] == "buy"]
    fill_sell_seq = [int(r[0]) for r in signal_rows if r[2] == "fill" and r[3] == "sell"]
    trade_hist_path = os.path.join(output_dir, f"run_{run_id}_trade_signal_hist.png")
    if fill_buy_seq or fill_sell_seq:
        plt.figure(figsize=(10, 4))
        plt.hist([fill_buy_seq, fill_sell_seq], bins=bins, label=["entry_buy_fill", "exit_sell_fill"], alpha=0.75)
        plt.title(f"Entry/Exit operation moments (hist) - run {run_id}")
        plt.xlabel("step")
        plt.ylabel("count")
        plt.legend()
        plt.tight_layout()
        plt.savefig(trade_hist_path)
        plt.close()

    # 2) Signal activations (buy/sell), whether filled or rejected.
    buy_activation_seq = [int(r[0]) for r in signal_rows if r[3] == "buy"]
    sell_activation_seq = [int(r[0]) for r in signal_rows if r[3] == "sell"]
    activation_hist_path = os.path.join(output_dir, f"run_{run_id}_signal_activation_hist.png")
    if buy_activation_seq or sell_activation_seq:
        plt.figure(figsize=(10, 4))
        plt.hist([buy_activation_seq, sell_activation_seq], bins=bins, label=["buy_signal", "sell_signal"], alpha=0.75)
        plt.title(f"Buy/Sell signal activations (hist) - run {run_id}")
        plt.xlabel("step")
        plt.ylabel("count")
        plt.legend()
        plt.tight_layout()
        plt.savefig(activation_hist_path)
        plt.close()

    out: Dict[str, str] = {}
    if os.path.exists(trade_hist_path):
        out["trade_signal_hist"] = trade_hist_path
    if os.path.exists(activation_hist_path):
        out["signal_activation_hist"] = activation_hist_path
    return out


def export_summary(
    output_dir: str,
    file_stem: str,
    metrics: Dict[str, float],
    equity_rows: List[Tuple],
    descriptor: Dict | None = None,
) -> Dict[str, str]:
    ensure_dir(output_dir)
    json_path = os.path.join(output_dir, f"{file_stem}_metrics.json")
    csv_path = os.path.join(output_dir, f"{file_stem}_equity.csv")
    report_path = os.path.join(output_dir, f"{file_stem}_report.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(metrics, f, ensure_ascii=False, indent=2)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["seq", "event_time", "equity"])
        for r in equity_rows:
            writer.writerow(r)
    report_payload = {
        "descriptor": descriptor or {},
        "metrics": metrics,
        "equity_points": len(equity_rows),
    }
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report_payload, f, ensure_ascii=False, indent=2)
    return {"metrics_json": json_path, "equity_csv": csv_path, "run_report_json": report_path}


def export_study_summary_table(output_dir: str, study_name: str, trials: List[Tuple]) -> Dict[str, str]:
    ensure_dir(output_dir)
    json_path = os.path.join(output_dir, f"study_{study_name}_summary.json")
    md_path = os.path.join(output_dir, f"study_{study_name}_summary.md")
    csv_path = os.path.join(output_dir, f"study_{study_name}_summary.csv")

    valid = [t for t in trials if t[3] is not None]
    if not valid:
        payload = {"study_name": study_name, "total_trials": len(trials), "valid_trials": 0}
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(f"# Study summary: {study_name}\n\nNo valid trials with objective.\n")
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["section", "trial_number", "objective", "params_json"])
        return {"study_summary_json": json_path, "study_summary_md": md_path, "study_summary_csv": csv_path}

    objectives = [float(t[3]) for t in valid]
    best = max(valid, key=lambda x: float(x[3]))
    worst = min(valid, key=lambda x: float(x[3]))
    mean_obj = float(sum(objectives) / len(objectives))
    median_obj = float(statistics.median(objectives))

    payload = {
        "study_name": study_name,
        "total_trials": len(trials),
        "valid_trials": len(valid),
        "objective_mean": mean_obj,
        "objective_median": median_obj,
        "best": {
            "trial_number": int(best[1]),
            "objective": float(best[3]),
            "params_json": str(best[4]),
            "state": best[2],
        },
        "worst": {
            "trial_number": int(worst[1]),
            "objective": float(worst[3]),
            "params_json": str(worst[4]),
            "state": worst[2],
        },
    }
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(f"# Study summary: {study_name}\n\n")
        f.write("## Final report table\n\n")
        f.write("| Scenario | Trial | Objective | Parameters |\n")
        f.write("|---|---:|---:|---|\n")
        f.write(f"| Best configuration | {int(best[1])} | {float(best[3]):.6f} | `{str(best[4])}` |\n")
        f.write(f"| Mean objective | N/A | {mean_obj:.6f} | N/A |\n")
        f.write(f"| Median objective | N/A | {median_obj:.6f} | N/A |\n")
        f.write(f"| Worst configuration | {int(worst[1])} | {float(worst[3]):.6f} | `{str(worst[4])}` |\n")

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["scenario", "trial_number", "objective", "params_json"])
        writer.writerow(["best_configuration", int(best[1]), float(best[3]), str(best[4])])
        writer.writerow(["mean_objective", "", mean_obj, ""])
        writer.writerow(["median_objective", "", median_obj, ""])
        writer.writerow(["worst_configuration", int(worst[1]), float(worst[3]), str(worst[4])])

    return {"study_summary_json": json_path, "study_summary_md": md_path, "study_summary_csv": csv_path}

