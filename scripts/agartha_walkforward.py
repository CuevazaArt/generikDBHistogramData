"""Walk-forward (out-of-sample) validation per symbol.

Para cada study existente:
  1. Lee la ventana completa del dataset (klines en DB).
  2. Split temporal: 70% train / 30% test.
  3. Re-corre Optuna SOLO sobre train -> mejores params train.
  4. Backtest fijo con esos params sobre test -> retorno OOS.
  5. Compara train_return vs test_return -> mide generalizacion.

Output: WALKFORWARD_REPORT.md con tabla per-symbol + estadistico global
(% de symbols donde test_return > 0, correlacion train/test, etc.).
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Optional, Tuple

from backtest.engine import EngineConfig
from backtest.registry import get_strategy
from backtest.runner import execute_and_persist


def _window_for_symbol(db: str, sym: str, interval: str) -> Tuple[int, int, int] | None:
    c = sqlite3.connect(db)
    try:
        row = c.execute(
            "SELECT MIN(open_time), MAX(open_time), COUNT(*) FROM klines WHERE symbol=? AND interval=?",
            (sym, interval),
        ).fetchone()
    finally:
        c.close()
    if not row or not row[2]:
        return None
    return int(row[0]), int(row[1]), int(row[2])


def _best_params_from_study(study_dir: Path) -> Dict | None:
    """Read trial_to_run.json best_params."""
    p = study_dir / "trial_to_run.json"
    if not p.exists():
        return None
    payload = json.loads(p.read_text(encoding="utf-8"))
    return payload.get("best_params") or {}


def _run_optuna_train(symbol: str, interval: str, start_ts: int, end_ts: int,
                      study_name: str) -> int:
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.call([
        sys.executable, "scripts/agartha_optuna_spectrum.py",
        "--db", "klines.db",
        "--symbol", symbol, "--interval", interval,
        "--start_ts", str(start_ts), "--end_ts", str(end_ts),
        "--initial_cash", "10", "--quote_order_qty_usdt", "10",
        "--max_cycles", "0",
        "--study", study_name,
        "--trials", "100", "--extreme", "20",
        "--output_root", "reports",
    ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _run_oos_backtest(symbol: str, interval: str, start_ts: int, end_ts: int,
                      params: Dict, initial_cash: float = 10.0) -> float | None:
    """Returns total_return (fraction) on OOS window."""
    strategy_cls = get_strategy("agartha")
    cfg = EngineConfig(
        db_path="klines.db",
        symbol=symbol, interval=interval,
        start_ts=start_ts, end_ts=end_ts,
        initial_cash=initial_cash,
        fee_rate=0.001, slippage_bps=10.0,
        events_mode="lite", snapshot_seconds=3600,
    )
    full_params = {
        "quote_order_qty_usdt": 10.0,
        "trailing_stop_pct": float(params.get("trailing_stop_pct", 30.0)),
        "activation_profit_pct": float(params.get("activation_profit_pct", 0.0)),
        "breakeven_lock_pct": float(params.get("breakeven_lock_pct", 0.0)),
        "max_holding_bars": 0,
        "partial_tp_pct": 0.0,
        "partial_tp_size_pct": 0.0,
        "max_cycles": 0,
        "reentry_cooldown_bars": 0,
        "entry_limit_offset_pct": float(params.get("entry_limit_offset_pct", 0.0)),
        "entry_limit_expiry_bars": 0,
        "entry_limit_reprice_on_expiry": False,
    }
    try:
        result = execute_and_persist(
            config=cfg, strategy_cls=strategy_cls, strategy_params=full_params,
        )
        return float(result.metrics.get("total_return", 0.0))
    except Exception:
        return None


def _walkforward_one(symbol_pair: str, interval: str = "15m",
                     train_frac: float = 0.7) -> Dict:
    """Returns dict con metricas para un symbol."""
    t0 = time.monotonic()
    win = _window_for_symbol("klines.db", symbol_pair, interval)
    if win is None:
        return {"symbol": symbol_pair, "status": "no_data"}
    start_ts, end_ts, n_bars = win
    if n_bars < 200:
        return {"symbol": symbol_pair, "status": "insufficient_bars", "n_bars": n_bars}
    split_ts = int(start_ts + (end_ts - start_ts) * train_frac)

    # 1. Optuna TRAIN
    train_study = f"agartha_{symbol_pair.lower()}_15m_wf_train"
    # Limpia stale db si existe
    train_dir = Path("reports/entregables/studies") / train_study
    opt_db = train_dir / "optuna.db"
    if opt_db.exists():
        try:
            opt_db.unlink()
        except Exception:
            pass
    rc = _run_optuna_train(symbol_pair, interval, start_ts, split_ts, train_study)
    if rc != 0:
        return {"symbol": symbol_pair, "status": "optuna_failed", "rc": rc}

    train_params = _best_params_from_study(train_dir)
    if not train_params:
        return {"symbol": symbol_pair, "status": "no_train_params"}
    # Lectura del mejor return TRAIN
    train_payload = json.loads((train_dir / "trial_to_run.json").read_text(encoding="utf-8"))
    train_return = float(train_payload.get("best_value", 0.0))

    # 2. OOS backtest
    test_return = _run_oos_backtest(symbol_pair, interval, split_ts + 1, end_ts, train_params)
    if test_return is None:
        return {"symbol": symbol_pair, "status": "oos_backtest_failed", "train_return": train_return}

    elapsed = time.monotonic() - t0
    return {
        "symbol": symbol_pair,
        "status": "ok",
        "n_bars": n_bars,
        "split_ts": split_ts,
        "train_return": train_return,
        "test_return": test_return,
        "params": train_params,
        "elapsed_sec": elapsed,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", required=True, help="Pares con quote, ej. BILLUSDT")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--train_frac", type=float, default=0.7)
    parser.add_argument("--output", default="reports/entregables/cross_studies/WALKFORWARD_REPORT.md")
    args = parser.parse_args()

    print(f"[walkforward] {len(args.symbols)} symbols | {args.workers} workers | train_frac={args.train_frac}", flush=True)
    t0 = time.monotonic()
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_walkforward_one, s, "15m", args.train_frac): s for s in args.symbols}
        for i, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            sym = r["symbol"]
            status = r["status"]
            if status == "ok":
                tr = r["train_return"] * 100
                te = r["test_return"] * 100
                print(f"  [{i:>3}/{len(args.symbols)}] {sym:<14} train={tr:+7.1f}% test={te:+7.1f}%", flush=True)
            else:
                print(f"  [{i:>3}/{len(args.symbols)}] {sym:<14} STATUS={status}", flush=True)
            results.append(r)

    # Reporte
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    ok_rows = [r for r in results if r["status"] == "ok"]
    pos_train = sum(1 for r in ok_rows if r["train_return"] > 0)
    pos_test = sum(1 for r in ok_rows if r["test_return"] > 0)
    train_pos_test_pos = sum(1 for r in ok_rows if r["train_return"] > 0 and r["test_return"] > 0)
    train_pos_test_neg = sum(1 for r in ok_rows if r["train_return"] > 0 and r["test_return"] <= 0)
    train_neg_test_pos = sum(1 for r in ok_rows if r["train_return"] <= 0 and r["test_return"] > 0)
    avg_train = sum(r["train_return"] for r in ok_rows) / max(1, len(ok_rows))
    avg_test = sum(r["test_return"] for r in ok_rows) / max(1, len(ok_rows))

    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write("# Walk-forward report (Agartha Alpha)\n\n")
        fh.write(f"Symbols analizados: {len(results)} (OK: {len(ok_rows)})\n\n")
        fh.write(f"- Train fraction: **{args.train_frac:.2f}** | Test: {1-args.train_frac:.2f}\n")
        fh.write(f"- Workers: {args.workers}\n")
        fh.write(f"- Tiempo total: {(time.monotonic()-t0)/60:.1f} min\n\n")
        fh.write("## Estadisticas globales\n\n")
        fh.write(f"- **Train positivos**: {pos_train}/{len(ok_rows)} ({pos_train/max(1,len(ok_rows))*100:.0f}%)\n")
        fh.write(f"- **Test positivos**: {pos_test}/{len(ok_rows)} ({pos_test/max(1,len(ok_rows))*100:.0f}%)\n")
        fh.write(f"- **Train+/Test+** (generalizan): {train_pos_test_pos}/{len(ok_rows)} ({train_pos_test_pos/max(1,len(ok_rows))*100:.0f}%)\n")
        fh.write(f"- **Train+/Test-** (overfit): {train_pos_test_neg}/{len(ok_rows)} ({train_pos_test_neg/max(1,len(ok_rows))*100:.0f}%)\n")
        fh.write(f"- Train-/Test+ (suerte test): {train_neg_test_pos}/{len(ok_rows)}\n")
        fh.write(f"- Avg train return: **{avg_train*100:+.2f}%**\n")
        fh.write(f"- Avg test return: **{avg_test*100:+.2f}%**\n")
        fh.write(f"- Decay (avg_test - avg_train): **{(avg_test-avg_train)*100:+.2f} pp**\n\n")
        fh.write("## Detalle por symbol\n\n")
        fh.write("| Symbol | n_bars | train | test | be | trailing | offset | status |\n")
        fh.write("|---|---:|---:|---:|---:|---:|---:|---|\n")
        for r in sorted(results, key=lambda x: -(x.get("test_return") or -999)):
            sym = r["symbol"]
            if r["status"] != "ok":
                fh.write(f"| {sym} | — | — | — | — | — | — | {r['status']} |\n")
                continue
            p = r["params"]
            fh.write(f"| {sym} | {r['n_bars']} | "
                     f"{r['train_return']*100:+.1f}% | {r['test_return']*100:+.1f}% | "
                     f"{p.get('breakeven_lock_pct', 0):.0f} | "
                     f"{p.get('trailing_stop_pct', 0):.1f}% | "
                     f"{p.get('entry_limit_offset_pct', 0):.0f}% | ok |\n")

    print(f"\n[walkforward] DONE. Report: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
