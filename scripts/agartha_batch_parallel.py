"""Parallel batch runner for agartha alpha studies.

Usa ProcessPoolExecutor con N workers. Cada worker ejecuta el pipeline
canonico completo (download + optuna + 3 runs) para UN symbol distinto.

SQLite shared (klines.db, bt_runs, bt_events, bt_equity) tendra contencion
de writes; mitigado por WAL mode + commits cortos. Optuna usa su propia
sqlite por study (optuna.db en la carpeta del study), sin contencion entre
workers. Tested con N=4-6 workers; mas alto puede degradar por lock waits.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Tuple


def _run_one(symbol: str, interval: str = "15m") -> Tuple[str, int, float]:
    """Run one symbol pipeline; return (symbol, rc, elapsed_sec)."""
    t0 = time.monotonic()
    env = dict(os.environ)
    env["PYTHONPATH"] = "."
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # Clear stale optuna.db
    studies_root = Path("reports/entregables/studies")
    for d in studies_root.glob(f"agartha_{symbol.lower()}*_15m_alpha_study"):
        opt_db = d / "optuna.db"
        if opt_db.exists():
            try:
                opt_db.unlink()
            except Exception:
                pass
    try:
        rc = subprocess.call(
            [
                sys.executable,
                "scripts/agartha_alpha_study.py",
                "--symbol", symbol,
                "--interval", interval,
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        rc = 99
    return symbol, rc, time.monotonic() - t0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--interval", default="15m")
    args = parser.parse_args()

    print(f"[parallel] {len(args.symbols)} symbols | {args.workers} workers", flush=True)
    t0 = time.monotonic()
    results = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_run_one, s, args.interval): s for s in args.symbols}
        done = 0
        for fut in as_completed(futures):
            sym, rc, elapsed = fut.result()
            done += 1
            flag = "OK " if rc == 0 else f"FL{rc:>2}"
            results.append((sym, rc, elapsed))
            avg = (time.monotonic() - t0) / done
            eta = avg * (len(args.symbols) - done) / args.workers
            print(f"  [{done:>3}/{len(args.symbols)}] {flag} {sym:<14} {elapsed:>6.1f}s "
                  f"(ETA ~{eta/60:.1f}min)", flush=True)

    total = time.monotonic() - t0
    ok = sum(1 for _, rc, _ in results if rc == 0)
    fails = [(s, rc) for s, rc, _ in results if rc != 0]
    print(f"\n=== BATCH PARALLEL SUMMARY ===")
    print(f"OK: {ok}/{len(results)} | total time: {total/60:.1f} min")
    if fails:
        print(f"FAILS:")
        for s, rc in fails:
            print(f"  {s}: rc={rc}")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
