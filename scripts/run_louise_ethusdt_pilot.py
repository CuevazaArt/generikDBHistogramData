"""Louise backtest on ETHUSDT with optional monthly chain and profit target.

Deliverables under reports/entregables/strict/<run_name>/:
  - equity_drawdown.png
  - RUN_SUMMARY.md
  - run_manifest.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from backtest.calendar_windows import monthly_windows
from backtest.engine import EngineConfig
from backtest.registry import get_strategy
from backtest.report_paths import strict_report_dir
from backtest.repro import git_snapshot
from backtest.runner import execute_and_persist
from backtest.storage import run_equity_curve, summarize_run

try:
    import matplotlib.pyplot as plt
except ImportError:  # pragma: no cover
    plt = None

DEFAULT_TARGET_PROFIT_PCT = 1.5
DEFAULT_MARGIN_DROP_FACTOR = 0.004
DEFAULT_QUOTE_ORDER_QTY_USDT = 8.0
DEFAULT_START_TS = 1704067200000  # 2024-01-01
DEFAULT_END_TS = 1779516000000  # 2026-05-23 (dataset ETH)


def _now_tag() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _drawdown_series(equity: List[float]) -> List[float]:
    peak = equity[0] if equity else 0.0
    out: List[float] = []
    for e in equity:
        peak = max(peak, e)
        out.append((peak - e) / peak * 100.0 if peak > 0 else 0.0)
    return out


def plot_equity_drawdown_combined(
    equity_rows: List[Tuple],
    output_path: str,
    *,
    title: str,
    max_points: int = 10_000,
) -> str:
    if plt is None:
        raise RuntimeError("Matplotlib no instalado. pip install -r requirements.txt")
    if not equity_rows:
        raise ValueError("Curva de equity vacía")

    rows = list(equity_rows)
    if len(rows) > max_points:
        from backtest.plots import _downsample_equity_rows

        rows = _downsample_equity_rows(
            [(int(r[0]), int(r[1]), float(r[2])) for r in rows],
            max_points,
        )

    times = [datetime.fromtimestamp(int(r[1]) / 1000.0, tz=timezone.utc) for r in rows]
    equity = [float(r[2]) for r in rows]
    dd_pct = _drawdown_series(equity)

    fig, ax1 = plt.subplots(figsize=(12, 5))
    ax1.plot(times, equity, color="#2563eb", linewidth=1.2, label="Equity (USDT)")
    ax1.set_xlabel("Tiempo (UTC)")
    ax1.set_ylabel("Equity (USDT)", color="#2563eb")
    ax1.tick_params(axis="y", labelcolor="#2563eb")
    ax1.grid(True, alpha=0.25)

    ax2 = ax1.twinx()
    ax2.plot(times, dd_pct, color="#dc2626", linewidth=1.0, alpha=0.85, label="Drawdown (%)")
    ax2.set_ylabel("Drawdown (%)", color="#dc2626")
    ax2.tick_params(axis="y", labelcolor="#dc2626")
    ax2.set_ylim(bottom=0)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper left")
    ax1.set_title(title)
    fig.autofmt_xdate()
    fig.tight_layout()
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    fig.savefig(output_path, dpi=120)
    plt.close(fig)
    return output_path


def _metrics_only(summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not summary:
        return {}
    m = summary.get("metrics", summary)
    if not isinstance(m, dict):
        return {}
    return {k: v for k, v in m.items() if k not in {"recent_events"}}


def _truncate_at_profit(
    rows: List[Tuple],
    *,
    baseline_equity: float,
    profit_target_usdt: float,
) -> Tuple[List[Tuple], Optional[int]]:
    target = float(baseline_equity) + float(profit_target_usdt)
    out: List[Tuple] = []
    hit_ts: Optional[int] = None
    for row in rows:
        eq = float(row[2])
        out.append(row)
        if eq >= target:
            hit_ts = int(row[1])
            break
    return out, hit_ts


def _run_chain(
    args: argparse.Namespace,
    strategy_params: Dict[str, Any],
    *,
    baseline_equity: float,
    profit_target_usdt: Optional[float],
) -> Dict[str, Any]:
    windows = monthly_windows(int(args.start_ts), int(args.end_ts))
    state: Optional[Dict[str, Any]] = None
    month_runs: List[Dict[str, Any]] = []
    combined_eq: List[Tuple] = []
    target_reached = False
    stop_window: Optional[str] = None
    stop_ts: Optional[int] = None
    last_run_id = 0
    last_metrics: Dict[str, Any] = {}

    for window_name, w_start, w_end in windows:
        initial_cash = float(args.initial_cash)
        if state:
            broker = state.get("broker", {})
            if isinstance(broker, dict):
                initial_cash = float(broker.get("cash", initial_cash))

        cfg = EngineConfig(
            db_path=args.db,
            symbol=str(args.symbol).upper(),
            interval=str(args.interval),
            start_ts=int(w_start),
            end_ts=int(w_end),
            initial_cash=initial_cash,
            fee_rate=float(args.fee_rate),
            slippage_bps=float(args.slippage_bps),
            loop_seconds=int(args.loop_seconds),
            events_mode="lite",
            snapshot_seconds=3600,
        )
        print(f"[louise-pilot] ventana {window_name} …", flush=True)
        result = execute_and_persist(
            config=cfg,
            strategy_cls=get_strategy("louise"),
            strategy_params=strategy_params,
            initial_state=state,
        )
        run_id = int(result.run_id)
        last_run_id = run_id
        summary = summarize_run(args.db, run_id=run_id)
        last_metrics = _metrics_only(summary)
        state = dict(result.final_state or {})
        eq_rows = run_equity_curve(args.db, run_id=run_id)

        if profit_target_usdt is not None and profit_target_usdt > 0:
            eq_rows, hit_ts = _truncate_at_profit(
                eq_rows,
                baseline_equity=baseline_equity,
                profit_target_usdt=profit_target_usdt,
            )
            if hit_ts is not None:
                target_reached = True
                stop_window = window_name
                stop_ts = hit_ts

        combined_eq.extend(eq_rows)
        month_runs.append(
            {
                "window": window_name,
                "start_ts": w_start,
                "end_ts": w_end,
                "run_id": run_id,
                "final_equity": float(state.get("final_equity", 0.0)),
                "profit_vs_baseline": float(state.get("final_equity", 0.0)) - baseline_equity,
                "metrics": last_metrics,
            }
        )

        if target_reached:
            print(
                f"[louise-pilot] objetivo +{profit_target_usdt} USDT alcanzado en {window_name}",
                flush=True,
            )
            break

    final_equity = float(state.get("final_equity", 0.0)) if state else float(args.initial_cash)
    return {
        "mode": "chain",
        "baseline_equity": baseline_equity,
        "profit_target_usdt": profit_target_usdt,
        "target_reached": target_reached,
        "stop_window": stop_window,
        "stop_ts": stop_ts,
        "final_equity": final_equity,
        "profit_usdt": final_equity - baseline_equity,
        "last_run_id": last_run_id,
        "last_metrics": last_metrics,
        "month_runs": month_runs,
        "combined_equity_rows": combined_eq,
        "windows_executed": len(month_runs),
        "windows_total": len(windows),
    }


def _write_run_summary(
    path: str,
    *,
    run_id: int,
    metrics: Dict[str, Any],
    strategy_params: Dict[str, Any],
    engine: Dict[str, Any],
    window: Dict[str, Any],
    chart_path: str,
    chain: Optional[Dict[str, Any]] = None,
) -> None:
    max_dd = float(metrics.get("max_drawdown", 0.0) or 0.0) * 100.0
    ret_pct = float(metrics.get("total_return", 0.0) or 0.0) * 100.0
    title = "Louise — resumen ETHUSDT"
    if chain:
        title += " (cadena mensual)"
    else:
        title += " (1 mes)"

    lines = [
        f"# {title}",
        "",
        "## Seteo",
        "",
        "| Parametro | Valor |",
        "|---|---:|",
        f"| `target_profit_pct` | {strategy_params['target_profit_pct']} |",
        f"| `margin_drop_factor` | {strategy_params['margin_drop_factor']} |",
        f"| `quote_order_qty_usdt` | {strategy_params['quote_order_qty_usdt']} |",
        "",
        "## Condiciones de la corrida",
        "",
        f"- **run_id (ultimo tramo):** `{run_id}`",
        f"- **estrategia:** `louise`",
        f"- **symbol:** `{engine['symbol']}`",
        f"- **interval (velas):** `{engine['interval']}`",
        f"- **loop_seconds:** `{engine.get('loop_seconds')}`",
        f"- **ventana global:** {_ms_to_iso(window['start_ts'])} → {_ms_to_iso(window['end_ts'])}",
        f"- **initial_cash (cadena):** {engine['initial_cash']} USDT",
        f"- **fee_rate:** {engine['fee_rate']}",
        f"- **slippage_bps:** {engine['slippage_bps']}",
        f"- **events_mode:** `{engine.get('events_mode', 'lite')}`",
        "- **gates:** ninguno",
        "",
    ]

    if chain:
        lines.extend(
            [
                "## Objetivo de beneficio",
                "",
                f"- **baseline equity:** {chain['baseline_equity']:.2f} USDT",
                f"- **objetivo:** +{chain['profit_target_usdt']:.2f} USDT "
                f"(equity >= {chain['baseline_equity'] + chain['profit_target_usdt']:.2f})",
                f"- **alcanzado:** {'si' if chain['target_reached'] else 'no'}",
                f"- **ventanas ejecutadas:** {chain['windows_executed']} / {chain['windows_total']}",
            ]
        )
        if chain.get("stop_window"):
            lines.append(f"- **ventana de corte:** `{chain['stop_window']}`")
        if chain.get("stop_ts"):
            lines.append(f"- **timestamp corte:** {_ms_to_iso(int(chain['stop_ts']))}")
        lines.extend(
            [
                "",
                f"- **beneficio acumulado:** {chain['profit_usdt']:+.2f} USDT",
                "",
                "## Tramos mensuales",
                "",
                "| Ventana | run_id | equity final | beneficio vs baseline |",
                "|---|---:|---:|---:|",
            ]
        )
        for m in chain["month_runs"]:
            lines.append(
                f"| {m['window']} | {m['run_id']} | {m['final_equity']:.2f} | {m['profit_vs_baseline']:+.2f} |"
            )
        lines.append("")

    lines.extend(
        [
            "## Resultados (ultimo tramo / acumulado)",
            "",
            "| Metrica | Valor |",
            "|---|---:|",
            f"| Equity inicial cadena | {chain['baseline_equity'] if chain else metrics.get('initial_cash', engine['initial_cash']):.2f} USDT |",
            f"| Equity final | {float(chain['final_equity'] if chain else metrics.get('final_equity', 0.0)):.2f} USDT |",
            f"| Beneficio / retorno | {(chain['profit_usdt'] if chain else float(metrics.get('final_equity', 0.0)) - float(engine['initial_cash'])):+.2f} USDT |",
            f"| Max drawdown (ultimo tramo) | {max_dd:.2f} % |",
            f"| Trades (ultimo tramo) | {int(metrics.get('num_trades', 0))} |",
            f"| Win rate (ultimo tramo) | {float(metrics.get('win_rate', 0.0)) * 100:.1f} % |",
            "",
            "## Grafica",
            "",
            f"![Equity y drawdown]({os.path.basename(chart_path)})",
            "",
        ]
    )
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description="Louise ETHUSDT — piloto o cadena mensual.")
    parser.add_argument("--db", default="klines.db")
    parser.add_argument("--symbol", default="ETHUSDT")
    parser.add_argument("--interval", default="1s")
    parser.add_argument("--start_ts", type=int, default=DEFAULT_START_TS)
    parser.add_argument("--end_ts", type=int, default=DEFAULT_END_TS)
    parser.add_argument("--initial_cash", type=float, default=1000.0)
    parser.add_argument("--fee_rate", type=float, default=0.001)
    parser.add_argument("--slippage_bps", type=float, default=2.0)
    parser.add_argument("--loop_seconds", type=int, default=3600)
    parser.add_argument("--target_profit_pct", type=float, default=DEFAULT_TARGET_PROFIT_PCT)
    parser.add_argument("--margin_drop_factor", type=float, default=DEFAULT_MARGIN_DROP_FACTOR)
    parser.add_argument("--quote_order_qty_usdt", type=float, default=DEFAULT_QUOTE_ORDER_QTY_USDT)
    parser.add_argument("--chain-by-month", action="store_true")
    parser.add_argument("--profit_target_usdt", type=float, default=None, help="Detener cadena al alcanzar este beneficio.")
    parser.add_argument("--output_root", default="reports")
    args = parser.parse_args()

    strategy_params = {
        "target_profit_pct": float(args.target_profit_pct),
        "margin_drop_factor": float(args.margin_drop_factor),
        "quote_order_qty_usdt": float(args.quote_order_qty_usdt),
    }

    suffix = "chain" if args.chain_by_month else "1m_pilot"
    run_name = f"louise_{args.symbol.lower()}_{suffix}_{_now_tag()}"
    out_dir = strict_report_dir(args.output_root, run_name)

    baseline_equity = float(args.initial_cash)
    chain_result: Optional[Dict[str, Any]] = None

    if args.chain_by_month:
        print(f"[louise-pilot] Cadena mensual {run_name} …", flush=True)
        chain_result = _run_chain(
            args,
            strategy_params,
            baseline_equity=baseline_equity,
            profit_target_usdt=args.profit_target_usdt,
        )
        run_id = int(chain_result["last_run_id"])
        metrics = dict(chain_result.get("last_metrics") or {})
        eq_rows = list(chain_result.get("combined_equity_rows") or [])
        chart_title = (
            f"Louise ETHUSDT — cadena — loop {args.loop_seconds}s — "
            f"TP {args.target_profit_pct}% — run {run_id}"
        )
    else:
        cfg = EngineConfig(
            db_path=args.db,
            symbol=str(args.symbol).upper(),
            interval=str(args.interval),
            start_ts=int(args.start_ts),
            end_ts=int(args.end_ts),
            initial_cash=float(args.initial_cash),
            fee_rate=float(args.fee_rate),
            slippage_bps=float(args.slippage_bps),
            loop_seconds=int(args.loop_seconds),
            events_mode="lite",
            snapshot_seconds=3600,
        )
        print(f"[louise-pilot] Corriendo {run_name} …", flush=True)
        result = execute_and_persist(
            config=cfg,
            strategy_cls=get_strategy("louise"),
            strategy_params=strategy_params,
        )
        run_id = int(result.run_id)
        metrics = _metrics_only(summarize_run(args.db, run_id=run_id))
        eq_rows = run_equity_curve(args.db, run_id=run_id)
        chart_title = f"Louise ETHUSDT — run {run_id}"

    chart_path = os.path.join(out_dir, "equity_drawdown.png")
    plot_equity_drawdown_combined(eq_rows, chart_path, title=chart_title)

    engine_snapshot = {
        "symbol": str(args.symbol).upper(),
        "interval": str(args.interval),
        "loop_seconds": int(args.loop_seconds),
        "initial_cash": float(args.initial_cash),
        "fee_rate": float(args.fee_rate),
        "slippage_bps": float(args.slippage_bps),
        "events_mode": "lite",
        "snapshot_seconds": 3600,
    }
    window = {"start_ts": int(args.start_ts), "end_ts": int(args.end_ts)}

    manifest: Dict[str, Any] = {
        "schema_version": 1,
        "run_name": run_name,
        "run_id": run_id,
        "strategy": "louise",
        "strategy_params": strategy_params,
        "engine": engine_snapshot,
        "window": window,
        "metrics_last_segment": metrics,
        "artifacts": {
            "equity_drawdown_chart": "equity_drawdown.png",
            "run_summary": "RUN_SUMMARY.md",
        },
        "reproducibility": {"git": git_snapshot()},
    }
    if chain_result:
        manifest["chain"] = {
            k: v
            for k, v in chain_result.items()
            if k not in {"combined_equity_rows", "last_metrics"}
        }

    with open(os.path.join(out_dir, "run_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    _write_run_summary(
        os.path.join(out_dir, "RUN_SUMMARY.md"),
        run_id=run_id,
        metrics=metrics,
        strategy_params=strategy_params,
        engine=engine_snapshot,
        window=window,
        chart_path=chart_path,
        chain=chain_result,
    )

    with open(os.path.join(out_dir, "MANIFEST.md"), "w", encoding="utf-8") as fh:
        fh.write(
            f"# Entregables — {run_name}\n\n"
            f"- `equity_drawdown.png`\n"
            f"- `RUN_SUMMARY.md`\n"
            f"- `run_manifest.json`\n\n"
            f"run_id (ultimo tramo): `{run_id}`\n"
        )

    final_eq = chain_result["final_equity"] if chain_result else metrics.get("final_equity")
    profit = chain_result["profit_usdt"] if chain_result else None
    msg = f"[louise-pilot] run_id={run_id} final_equity={final_eq}"
    if profit is not None:
        msg += f" profit={profit:+.2f} USDT target_reached={chain_result.get('target_reached')}"
    print(msg)
    print(f"[louise-pilot] Entregables: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
