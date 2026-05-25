"""Agartha pilot: BILLUSDT (Alpha 15m) full history, single instance, default preset.

Entregables bajo reports/entregables/strict/<run_name>/:
  - equity_drawdown.png
  - RUN_SUMMARY.md
  - run_manifest.json
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

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
    eq_rows: List[Tuple],
    output_path: str,
    *,
    title: str,
    max_points: int = 10_000,
) -> str:
    if plt is None:
        raise RuntimeError("Matplotlib no instalado.")
    if not eq_rows:
        raise ValueError("Curva de equity vacia.")
    rows = list(eq_rows)
    if len(rows) > max_points:
        from backtest.plots import _downsample_equity_rows
        rows = _downsample_equity_rows(
            [(int(r[0]), int(r[1]), float(r[2])) for r in rows], max_points
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


def _window_from_db(db: str, symbol: str, interval: str) -> Tuple[int, int]:
    c = sqlite3.connect(db)
    try:
        row = c.execute(
            "SELECT MIN(open_time), MAX(open_time) FROM klines WHERE symbol=? AND interval=?",
            (symbol, interval),
        ).fetchone()
    finally:
        c.close()
    if not row or row[0] is None:
        raise SystemExit(f"No data for {symbol}/{interval} in {db}")
    return int(row[0]), int(row[1])


def _write_run_summary(
    path: str,
    *,
    run_id: int,
    metrics: Dict[str, Any],
    strategy_params: Dict[str, Any],
    engine: Dict[str, Any],
    window: Dict[str, Any],
    chart_path: str,
    extra_state: Dict[str, Any],
) -> None:
    max_dd = float(metrics.get("max_drawdown", 0.0) or 0.0) * 100.0
    ret_pct = float(metrics.get("total_return", 0.0) or 0.0) * 100.0
    lines = [
        "# Agartha BILLUSDT (Alpha 15m) - pilot",
        "",
        "## Seteo",
        "",
        "| Parametro | Valor |",
        "|---|---:|",
    ]
    for k, v in strategy_params.items():
        lines.append(f"| `{k}` | {v} |")
    lines += [
        "",
        "## Condiciones",
        "",
        f"- **run_id:** `{run_id}`",
        f"- **estrategia:** `agartha`",
        f"- **symbol:** `{engine['symbol']}` (Alpha: `ALPHA_953USDT`, BILL / Billions Network)",
        f"- **interval:** `{engine['interval']}`",
        f"- **ventana:** {_ms_to_iso(window['start_ts'])} -> {_ms_to_iso(window['end_ts'])}",
        f"- **initial_cash:** {engine['initial_cash']} USDT",
        f"- **fee_rate:** {engine['fee_rate']}",
        f"- **slippage_bps:** {engine['slippage_bps']}",
        "",
        "## Resultados",
        "",
        "| Metrica | Valor |",
        "|---|---:|",
        f"| Equity inicial | {metrics.get('initial_cash', engine['initial_cash']):.4f} USDT |",
        f"| Equity final | {float(metrics.get('final_equity', 0.0)):.4f} USDT |",
        f"| Retorno total | {ret_pct:+.2f} % |",
        f"| Max drawdown | {max_dd:.2f} % |",
        f"| Trades | {int(metrics.get('num_trades', 0))} |",
        f"| Win rate | {float(metrics.get('win_rate', 0.0)) * 100:.1f} % |",
        f"| Sharpe | {float(metrics.get('sharpe', 0.0)):.3f} |",
        "",
        "## Estado interno final Agartha",
        "",
        "| Campo | Valor |",
        "|---|---:|",
    ]
    for k, v in extra_state.items():
        lines.append(f"| `{k}` | {v} |")
    lines += [
        "",
        "## Grafica",
        "",
        f"![Equity y drawdown]({os.path.basename(chart_path)})",
        "",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description="Agartha pilot: BILLUSDT Alpha 15m.")
    parser.add_argument("--db", default="klines.db")
    parser.add_argument("--symbol", default="BILLUSDT")
    parser.add_argument("--interval", default="15m")
    parser.add_argument("--initial_cash", type=float, default=100.0,
                        help="Cash inicial. 100 USDT alcanza para 1 instancia Agartha de 10 USDT con margen.")
    parser.add_argument("--quote_order_qty_usdt", type=float, default=10.0)
    parser.add_argument("--trailing_stop_pct", type=float, default=30.0)
    parser.add_argument("--activation_profit_pct", type=float, default=0.0)
    parser.add_argument("--max_holding_bars", type=int, default=0)
    parser.add_argument("--breakeven_lock_pct", type=float, default=0.0)
    parser.add_argument("--partial_tp_pct", type=float, default=0.0)
    parser.add_argument("--partial_tp_size_pct", type=float, default=0.0)
    parser.add_argument("--max_cycles", type=int, default=0,
                        help="0=ilimitado (default, ciclo continuo); 1=single-shot.")
    parser.add_argument("--reentry_cooldown_bars", type=int, default=0)
    parser.add_argument("--entry_limit_offset_pct", type=float, default=0.0,
                        help="LIMIT BUY a X%% debajo del precio actual; 0=compra inmediata.")
    parser.add_argument("--entry_limit_expiry_bars", type=int, default=0,
                        help="Barras antes de expirar la LIMIT pendiente (0=GTC).")
    parser.add_argument("--entry_limit_reprice_on_expiry", action="store_true")
    parser.add_argument("--fee_rate", type=float, default=0.001)
    parser.add_argument("--slippage_bps", type=float, default=10.0,
                        help="Alpha tokens: slippage mas alto que spot maduro.")
    parser.add_argument("--output_root", default="reports")
    args = parser.parse_args()

    start_ts, end_ts = _window_from_db(args.db, args.symbol, args.interval)
    strategy_params = {
        "quote_order_qty_usdt": float(args.quote_order_qty_usdt),
        "trailing_stop_pct": float(args.trailing_stop_pct),
        "activation_profit_pct": float(args.activation_profit_pct),
        "max_holding_bars": int(args.max_holding_bars),
        "breakeven_lock_pct": float(args.breakeven_lock_pct),
        "partial_tp_pct": float(args.partial_tp_pct),
        "partial_tp_size_pct": float(args.partial_tp_size_pct),
        "max_cycles": int(args.max_cycles),
        "reentry_cooldown_bars": int(args.reentry_cooldown_bars),
        "entry_limit_offset_pct": float(args.entry_limit_offset_pct),
        "entry_limit_expiry_bars": int(args.entry_limit_expiry_bars),
        "entry_limit_reprice_on_expiry": bool(args.entry_limit_reprice_on_expiry),
    }
    cfg = EngineConfig(
        db_path=args.db,
        symbol=args.symbol.upper(),
        interval=args.interval,
        start_ts=start_ts,
        end_ts=end_ts,
        initial_cash=float(args.initial_cash),
        fee_rate=float(args.fee_rate),
        slippage_bps=float(args.slippage_bps),
        events_mode="lite",
        snapshot_seconds=3600,
    )
    run_name = f"agartha_{args.symbol.lower()}_{args.interval}_pilot_{_now_tag()}"
    out_dir = strict_report_dir(args.output_root, run_name)

    print(f"[agartha-pilot] {run_name} ({_ms_to_iso(start_ts)} -> {_ms_to_iso(end_ts)})", flush=True)
    strategy_cls = get_strategy("agartha")
    result = execute_and_persist(
        config=cfg, strategy_cls=strategy_cls, strategy_params=strategy_params,
    )
    run_id = int(result.run_id)
    summary = summarize_run(args.db, run_id=run_id) or {}
    metrics = dict(summary.get("metrics") or summary or {})
    eq_rows = run_equity_curve(args.db, run_id=run_id)
    chart_path = os.path.join(out_dir, "equity_drawdown.png")
    plot_equity_drawdown_combined(
        eq_rows, chart_path,
        title=f"Agartha BILLUSDT (Alpha 15m) - run {run_id}",
    )

    engine_snapshot = {
        "symbol": cfg.symbol, "interval": cfg.interval,
        "initial_cash": cfg.initial_cash,
        "fee_rate": cfg.fee_rate, "slippage_bps": cfg.slippage_bps,
        "events_mode": cfg.events_mode, "snapshot_seconds": cfg.snapshot_seconds,
    }
    window = {"start_ts": start_ts, "end_ts": end_ts}
    final_state = result.final_state.get("strategy", {}) if result.final_state else {}
    manifest = {
        "schema_version": 1, "run_name": run_name, "run_id": run_id,
        "strategy": "agartha", "strategy_params": strategy_params,
        "engine": engine_snapshot, "window": window,
        "metrics": {k: v for k, v in metrics.items() if k != "recent_events"},
        "agartha_final_state": final_state,
        "artifacts": {
            "equity_drawdown_chart": "equity_drawdown.png",
            "run_summary": "RUN_SUMMARY.md",
        },
        "reproducibility": {"git": git_snapshot()},
    }
    with open(os.path.join(out_dir, "run_manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2, default=str)
    _write_run_summary(
        os.path.join(out_dir, "RUN_SUMMARY.md"),
        run_id=run_id, metrics=metrics, strategy_params=strategy_params,
        engine=engine_snapshot, window=window, chart_path=chart_path,
        extra_state=final_state,
    )
    with open(os.path.join(out_dir, "MANIFEST.md"), "w", encoding="utf-8") as fh:
        fh.write(
            f"# Entregables - {run_name}\n\n"
            f"- `equity_drawdown.png`\n- `RUN_SUMMARY.md`\n- `run_manifest.json`\n\n"
            f"run_id: `{run_id}`\n"
        )
    print(
        f"[agartha-pilot] run_id={run_id} final_equity={metrics.get('final_equity')} "
        f"num_trades={metrics.get('num_trades')}"
    )
    print(f"[agartha-pilot] Entregables: {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
