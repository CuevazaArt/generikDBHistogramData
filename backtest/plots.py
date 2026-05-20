"""Plot and export helpers for backtest analysis."""
import csv
import json
import statistics
import os
from datetime import datetime, timezone
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


def plot_monthly_return_spectrum(equity_rows: List[Tuple], output_dir: str, run_id: int) -> str:
    if plt is None:
        raise RuntimeError("Matplotlib is not installed. Run: pip install -r requirements.txt")
    ensure_dir(output_dir)
    if not equity_rows:
        return ""
    monthly: Dict[str, List[float]] = {}
    for _, ts, eq in equity_rows:
        if ts is None:
            continue
        key = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc).strftime("%Y-%m")
        monthly.setdefault(key, []).append(float(eq))
    keys = sorted(monthly.keys())
    if not keys:
        return ""
    rets = []
    for k in keys:
        arr = monthly[k]
        if not arr or arr[0] == 0:
            rets.append(0.0)
        else:
            rets.append((arr[-1] - arr[0]) / arr[0] * 100.0)
    out = os.path.join(output_dir, f"run_{run_id}_monthly_return_spectrum.png")
    plt.figure(figsize=(12, 4))
    plt.bar(keys, rets)
    plt.xticks(rotation=45, ha="right")
    plt.ylabel("Monthly return %")
    plt.title(f"Monthly return spectrum - run {run_id}")
    plt.tight_layout()
    plt.savefig(out)
    plt.close()
    return out


def _monthly_return_pairs(equity_rows: List[Tuple]) -> List[Tuple[str, float]]:
    monthly: Dict[str, List[float]] = {}
    for _seq, ts, eq in equity_rows:
        if ts is None:
            continue
        key = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc).strftime("%Y-%m")
        monthly.setdefault(key, []).append(float(eq))
    pairs: List[Tuple[str, float]] = []
    for key in sorted(monthly.keys()):
        arr = monthly[key]
        if not arr or arr[0] == 0:
            pairs.append((key, 0.0))
        else:
            pairs.append((key, (arr[-1] - arr[0]) / arr[0]))
    return pairs


def _interval_to_seconds(interval: str) -> int:
    s = (interval or "").strip().lower()
    if not s:
        return 0
    try:
        if s.endswith("m"):
            return int(s[:-1]) * 60
        if s.endswith("h"):
            return int(s[:-1]) * 3600
        if s.endswith("d"):
            return int(s[:-1]) * 86400
        if s.endswith("w"):
            return int(s[:-1]) * 7 * 86400
    except Exception:
        return 0
    return 0


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
    table_md_path = os.path.join(output_dir, f"{file_stem}_final_table.md")
    table_csv_path = os.path.join(output_dir, f"{file_stem}_final_table.csv")
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
    desc = descriptor or {}
    with open(table_md_path, "w", encoding="utf-8") as f:
        f.write(f"# Final run table: {file_stem}\n\n")
        f.write("| Metric | Value |\n")
        f.write("|---|---:|\n")
        f.write(f"| strategy_name | {desc.get('strategy_name', '')} |\n")
        f.write(f"| symbol | {desc.get('symbol', '')} |\n")
        f.write(f"| interval | {desc.get('interval', '')} |\n")
        f.write(f"| first_event_iso_utc | {desc.get('first_event_iso_utc', '')} |\n")
        f.write(f"| last_event_iso_utc | {desc.get('last_event_iso_utc', '')} |\n")
        f.write(f"| event_count | {desc.get('event_count', '')} |\n")
        f.write(f"| capital_utilization_avg_pct | {desc.get('capital_utilization_avg_pct', '')} |\n")
        f.write(f"| capital_utilization_max_pct | {desc.get('capital_utilization_max_pct', '')} |\n")
        f.write(f"| max_open_orders_simultaneous | {desc.get('max_open_orders_simultaneous', '')} |\n")
        for k, v in metrics.items():
            f.write(f"| {k} | {v} |\n")
    with open(table_csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["metric", "value"])
        writer.writerow(["strategy_name", desc.get("strategy_name", "")])
        writer.writerow(["symbol", desc.get("symbol", "")])
        writer.writerow(["interval", desc.get("interval", "")])
        writer.writerow(["first_event_iso_utc", desc.get("first_event_iso_utc", "")])
        writer.writerow(["last_event_iso_utc", desc.get("last_event_iso_utc", "")])
        writer.writerow(["event_count", desc.get("event_count", "")])
        writer.writerow(["capital_utilization_avg_pct", desc.get("capital_utilization_avg_pct", "")])
        writer.writerow(["capital_utilization_max_pct", desc.get("capital_utilization_max_pct", "")])
        writer.writerow(["max_open_orders_simultaneous", desc.get("max_open_orders_simultaneous", "")])
        for k, v in metrics.items():
            writer.writerow([k, v])
    return {
        "metrics_json": json_path,
        "equity_csv": csv_path,
        "run_report_json": report_path,
        "final_table_md": table_md_path,
        "final_table_csv": table_csv_path,
    }


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
        best_params = {}
        worst_params = {}
        try:
            best_params = json.loads(str(best[4]))
        except Exception:
            pass
        try:
            worst_params = json.loads(str(worst[4]))
        except Exception:
            pass
        f.write("| Scenario | Trial | Objective | ProfitFactor | MarginDropFactor | DurationSec | Parameters |\n")
        f.write("|---|---:|---:|---:|---:|---:|---|\n")
        f.write(
            f"| Best configuration | {int(best[1])} | {float(best[3]):.6f} | {best_params.get('profit_factor', '')} | {best_params.get('margin_drop_factor', '')} | {best[7] if best[7] is not None else ''} | `{str(best[4])}` |\n"
        )
        f.write(f"| Mean objective | N/A | {mean_obj:.6f} |  |  |  |  |\n")
        f.write(f"| Median objective | N/A | {median_obj:.6f} |  |  |  |  |\n")
        f.write(
            f"| Worst configuration | {int(worst[1])} | {float(worst[3]):.6f} | {worst_params.get('profit_factor', '')} | {worst_params.get('margin_drop_factor', '')} | {worst[7] if worst[7] is not None else ''} | `{str(worst[4])}` |\n"
        )

    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["scenario", "trial_number", "objective", "profit_factor", "margin_drop_factor", "duration_sec", "params_json"])
        writer.writerow([
            "best_configuration",
            int(best[1]),
            float(best[3]),
            best_params.get("profit_factor", ""),
            best_params.get("margin_drop_factor", ""),
            best[7] if best[7] is not None else "",
            str(best[4]),
        ])
        writer.writerow(["mean_objective", "", mean_obj, "", "", "", ""])
        writer.writerow(["median_objective", "", median_obj, "", "", "", ""])
        writer.writerow([
            "worst_configuration",
            int(worst[1]),
            float(worst[3]),
            worst_params.get("profit_factor", ""),
            worst_params.get("margin_drop_factor", ""),
            worst[7] if worst[7] is not None else "",
            str(worst[4]),
        ])

    return {"study_summary_json": json_path, "study_summary_md": md_path, "study_summary_csv": csv_path}


def plot_monthly_return_heatmap(equity_rows: List[Tuple], output_dir: str, run_id: int) -> str:
    if plt is None:
        raise RuntimeError("Matplotlib is not installed. Run: pip install -r requirements.txt")
    ensure_dir(output_dir)
    if not equity_rows:
        return ""
    monthly: Dict[str, List[float]] = {}
    for _, ts, eq in equity_rows:
        if ts is None:
            continue
        key = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc).strftime("%Y-%m")
        monthly.setdefault(key, []).append(float(eq))
    keys = sorted(monthly.keys())
    if not keys:
        return ""
    values = []
    for k in keys:
        arr = monthly[k]
        if not arr or arr[0] == 0:
            values.append(0.0)
        else:
            values.append((arr[-1] - arr[0]) / arr[0] * 100.0)
    out = os.path.join(output_dir, f"run_{run_id}_monthly_return_heatmap.png")
    plt.figure(figsize=(max(8, len(values) * 0.8), 2.5))
    matrix = [values]
    im = plt.imshow(matrix, aspect="auto", cmap="RdYlGn")
    plt.colorbar(im, label="Monthly return %")
    plt.yticks([0], ["returns"])
    plt.xticks(range(len(keys)), keys, rotation=45, ha="right")
    plt.title(f"Monthly return heatmap - run {run_id}")
    plt.tight_layout()
    plt.savefig(out)
    plt.close()
    return out


def plot_fill_activity_heatmap(event_rows: List[Tuple], output_dir: str, run_id: int) -> str:
    if plt is None:
        raise RuntimeError("Matplotlib is not installed. Run: pip install -r requirements.txt")
    ensure_dir(output_dir)
    grid = [[0 for _ in range(24)] for _ in range(7)]
    has_data = False
    for _seq, ts, event_type, side, _cash, _equity, _payload in event_rows:
        if ts is None or event_type != "fill" or side not in ("buy", "sell"):
            continue
        d = datetime.fromtimestamp(int(ts) / 1000.0, tz=timezone.utc)
        grid[d.weekday()][d.hour] += 1
        has_data = True
    if not has_data:
        return ""
    out = os.path.join(output_dir, f"run_{run_id}_fill_activity_heatmap.png")
    plt.figure(figsize=(11, 4))
    im = plt.imshow(grid, aspect="auto", cmap="viridis")
    plt.colorbar(im, label="fills count")
    plt.yticks(range(7), ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])
    plt.xticks(range(0, 24, 2), range(0, 24, 2))
    plt.xlabel("UTC hour")
    plt.title(f"Fill activity heatmap (weekday/hour) - run {run_id}")
    plt.tight_layout()
    plt.savefig(out)
    plt.close()
    return out


def plot_optuna_param_heatmap(trials: List[Tuple], output_dir: str, study_name: str) -> str:
    if plt is None:
        raise RuntimeError("Matplotlib is not installed. Run: pip install -r requirements.txt")
    ensure_dir(output_dir)
    xs: List[float] = []
    ys: List[float] = []
    cs: List[float] = []
    for t in trials:
        if t[3] is None:
            continue
        try:
            p = json.loads(str(t[4]))
        except Exception:
            continue
        if p.get("profit_factor") is None or p.get("margin_drop_factor") is None:
            continue
        xs.append(float(p["profit_factor"]))
        ys.append(float(p["margin_drop_factor"]))
        cs.append(float(t[3]))
    if not xs:
        return ""
    out = os.path.join(output_dir, f"study_{study_name}_param_heatmap.png")
    plt.figure(figsize=(8, 5))
    hb = plt.hexbin(xs, ys, C=cs, reduce_C_function=statistics.mean, gridsize=22, cmap="plasma")
    plt.colorbar(hb, label="avg objective")
    plt.xlabel("profit_factor")
    plt.ylabel("margin_drop_factor")
    plt.title(f"Optuna parameter heatmap - {study_name}")
    plt.tight_layout()
    plt.savefig(out)
    plt.close()
    return out


def export_run_bot_summary(
    output_dir: str,
    file_stem: str,
    descriptor: Dict,
    metrics: Dict[str, float],
    bot_description: str,
    optuna_summary: str,
) -> str:
    ensure_dir(output_dir)
    out = os.path.join(output_dir, f"{file_stem}_bot_summary.md")
    cfg = {}
    try:
        cfg = json.loads(descriptor.get("config_json") or "{}")
    except Exception:
        cfg = {}
    strategy_cfg = cfg.get("strategy", {}) if isinstance(cfg, dict) else {}
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# Bot summary - {file_stem}\n\n")
        f.write("## Bot description\n\n")
        f.write(bot_description.strip() + "\n\n")
        f.write("## Setup used\n\n")
        f.write(f"- strategy: `{descriptor.get('strategy_name','')}`\n")
        f.write(f"- symbol: `{descriptor.get('symbol','')}`\n")
        f.write(f"- interval: `{descriptor.get('interval','')}`\n")
        f.write(f"- period_start_utc: `{descriptor.get('first_event_iso_utc','')}`\n")
        f.write(f"- period_end_utc: `{descriptor.get('last_event_iso_utc','')}`\n")
        f.write(f"- initial_cash: `{descriptor.get('initial_cash','')}`\n")
        for k, v in strategy_cfg.items():
            f.write(f"- {k}: `{v}`\n")
        f.write("\n## Performance summary\n\n")
        for k, v in metrics.items():
            f.write(f"- {k}: `{v}`\n")
        f.write(f"- capital_utilization_avg_pct: `{descriptor.get('capital_utilization_avg_pct','')}`\n")
        f.write(f"- capital_utilization_max_pct: `{descriptor.get('capital_utilization_max_pct','')}`\n")
        f.write(f"- max_open_orders_simultaneous: `{descriptor.get('max_open_orders_simultaneous','')}`\n")
        f.write("\n## Optuna usage (general)\n\n")
        f.write(optuna_summary.strip() + "\n")
    return out


def export_run_integrated_report(
    output_dir: str,
    file_stem: str,
    descriptor: Dict,
    metrics: Dict[str, float],
    equity_rows: List[Tuple],
    graph_paths: Dict[str, str],
) -> str:
    ensure_dir(output_dir)
    out = os.path.join(output_dir, f"{file_stem}_integrated_report.md")

    cfg = {}
    try:
        cfg = json.loads(descriptor.get("config_json") or "{}")
    except Exception:
        cfg = {}
    strategy_cfg = cfg.get("strategy", {}) if isinstance(cfg, dict) else {}
    engine_cfg = cfg.get("engine", {}) if isinstance(cfg, dict) else {}
    interval_seconds = _interval_to_seconds(str(descriptor.get("interval", "")))

    total_return = float(metrics.get("total_return", 0.0))
    drawdown = float(metrics.get("max_drawdown", 0.0))
    initial_cash = float(metrics.get("initial_cash", 0.0))
    final_equity = float(metrics.get("final_equity", 0.0))
    num_trades = int(float(metrics.get("num_trades", 0.0)))
    util_avg = float(descriptor.get("capital_utilization_avg_pct", 0.0) or 0.0)
    util_max = float(descriptor.get("capital_utilization_max_pct", 0.0) or 0.0)

    monthly_pairs = _monthly_return_pairs(equity_rows)
    pos_months = len([m for _, m in monthly_pairs if m > 0])
    neg_months = len([m for _, m in monthly_pairs if m < 0])
    total_months = len(monthly_pairs)
    best_month = max(monthly_pairs, key=lambda x: x[1]) if monthly_pairs else ("N/A", 0.0)
    worst_month = min(monthly_pairs, key=lambda x: x[1]) if monthly_pairs else ("N/A", 0.0)

    if total_return >= 0.18 and drawdown <= 0.18:
        verdict = "Bueno para uso real controlado."
    elif total_return > 0 and drawdown <= 0.28:
        verdict = "Aceptable, pero con monitoreo frecuente."
    else:
        verdict = "No recomendado en su forma actual para operar en vivo."

    def _embed_image(title: str, key: str) -> str:
        path = str(graph_paths.get(key, "") or "")
        if not path:
            return ""
        normalized = path.replace("\\", "/")
        rel = os.path.basename(normalized)
        return f"### {title}\n\n![{title}]({rel})\n\n"

    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# Reporte integrado de corrida - {file_stem}\n\n")
        f.write("## Resumen rapido\n\n")
        f.write("| Dato | Valor |\n")
        f.write("|---|---:|\n")
        f.write(f"| Capital inicial | {initial_cash:.2f} |\n")
        f.write(f"| Capital final | {final_equity:.2f} |\n")
        f.write(f"| Retorno total | {total_return * 100.0:.2f}% |\n")
        f.write(f"| Caida maxima | {drawdown * 100.0:.2f}% |\n")
        f.write(f"| Operaciones | {num_trades} |\n")
        f.write(f"| Uso de capital (promedio / max) | {util_avg:.2f}% / {util_max:.2f}% |\n")
        f.write(f"| Veredicto | {verdict} |\n\n")

        f.write("## Contexto de la corrida\n\n")
        f.write("| Campo | Valor |\n")
        f.write("|---|---|\n")
        f.write(f"| Estrategia | `{descriptor.get('strategy_name', 'dorothy')}` |\n")
        f.write(f"| Simbolo | `{descriptor.get('symbol', '')}` |\n")
        f.write(f"| Timeframe | `{descriptor.get('interval', '')}` |\n")
        f.write(f"| Periodo | `{descriptor.get('first_event_iso_utc', '')}` -> `{descriptor.get('last_event_iso_utc', '')}` |\n")
        configured_loop = engine_cfg.get("loop_seconds")
        if configured_loop is not None:
            f.write(f"| Loop configurado | `1 ciclo cada {configured_loop} segundos` |\n")
        if interval_seconds > 0:
            f.write(f"| Loop por vela (timeframe) | `1 ciclo cada {interval_seconds} segundos` |\n")
        f.write("\n")

        if strategy_cfg:
            f.write("## Parametros usados (estrategia)\n\n")
            f.write("| Parametro | Valor |\n")
            f.write("|---|---:|\n")
            for k, v in strategy_cfg.items():
                f.write(f"| `{k}` | `{v}` |\n")
            f.write("\n")

        f.write("## Rango Optuna para replicar\n\n")
        if str(descriptor.get("strategy_name", "")).strip().lower() == "dorothy":
            f.write("| Parametro | Min | Max | Paso |\n")
            f.write("|---|---:|---:|---:|\n")
            f.write("| `profit_factor` | 0.005 | 0.08 | 0.0005 |\n")
            f.write("| `margin_drop_factor` | 0.001 | 0.02 | 0.00025 |\n")
            f.write("| `quote_order_qty_usdt` | 6 | 10 | 1 |\n\n")
            f.write("- Fijos: `min_order_notional=6`, `max_order_notional=10`, `max_active_orders=200`\n")
            f.write("- Objetivo de optimizacion: `maximizar total_return`\n\n")
        else:
            f.write("- Espacio de Optuna no definido para esta estrategia en este reporte.\n\n")

        f.write("## Lectura practica\n\n")
        f.write(f"- Punto fuerte: cierre anual positivo (`{total_return * 100.0:.2f}%`).\n")
        f.write(f"- Riesgo relevante: retroceso maximo de `{drawdown * 100.0:.2f}%`.\n")
        if total_months > 0:
            f.write(
                f"- Regularidad mensual: `{pos_months}/{total_months}` meses positivos y `{neg_months}/{total_months}` negativos.\n"
                f"- Mejor mes: `{best_month[0]}` ({best_month[1] * 100.0:.2f}%). Peor mes: `{worst_month[0]}` ({worst_month[1] * 100.0:.2f}%).\n"
            )
        f.write("- Uso sugerido: operar con tamano moderado y revision semanal.\n")
        f.write("- Evitar: aumentar capital agresivamente despues de rachas cortas.\n\n")

        f.write("## Graficas clave\n\n")
        f.write(_embed_image("Curva de capital", "equity"))
        f.write(_embed_image("Caida temporal (drawdown)", "drawdown"))
        f.write(_embed_image("Distribucion de retornos", "returns_hist"))
        f.write(_embed_image("Entradas y salidas ejecutadas", "trade_signal_hist"))
        f.write(_embed_image("Activacion de senales", "signal_activation_hist"))
        f.write(_embed_image("Espectro mensual", "monthly_return_spectrum"))
        f.write(_embed_image("Heatmap mensual", "monthly_return_heatmap"))
        f.write(_embed_image("Actividad por dia/hora", "fill_activity_heatmap"))

        f.write("## Conclusion\n\n")
        f.write(f"- Decision sugerida: **{verdict}**\n")
        f.write("- Recomendacion operativa: priorizar control de riesgo y constancia en el seteo.\n\n")

        f.write("## Archivos generados\n\n")
        for key, path in sorted(graph_paths.items()):
            f.write(f"- `{key}`: `{path}`\n")

    return out


def export_study_optuna_summary(output_dir: str, study_name: str, summary_payload: Dict) -> str:
    ensure_dir(output_dir)
    out = os.path.join(output_dir, f"study_{study_name}_optuna_summary.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# Optuna summary - {study_name}\n\n")
        f.write("- sampler: default TPE sampler\n")
        f.write("- objective: maximize `total_return`\n")
        f.write("- search space: `profit_factor`, `margin_drop_factor`, `quote_order_qty_usdt (6-10 step 1)`\n")
        f.write("- fixed constraints: `min_order_notional=6`, `max_order_notional=10`, `max_active_orders=200`\n")
        f.write(f"- total_trials: {summary_payload.get('total_trials','')}\n")
        f.write(f"- valid_trials: {summary_payload.get('valid_trials','')}\n")
        f.write(f"- objective_mean: {summary_payload.get('objective_mean','')}\n")
        f.write(f"- objective_median: {summary_payload.get('objective_median','')}\n")
        best = summary_payload.get("best", {})
        worst = summary_payload.get("worst", {})
        f.write(f"- best_trial: {best.get('trial_number','')} objective={best.get('objective','')}\n")
        f.write(f"- worst_trial: {worst.get('trial_number','')} objective={worst.get('objective','')}\n")
    return out

