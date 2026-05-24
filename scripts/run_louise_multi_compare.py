"""Run Louise accumulate-only chain on XRP, BTC, BNB for comparison."""
from __future__ import annotations

import subprocess
import sys

SYMBOLS = ("XRPUSDT", "BTCUSDT", "BNBUSDT")
COMMON = [
    sys.executable,
    "scripts/run_louise_ethusdt_pilot.py",
    "--loop_seconds",
    "29",
    "--target_profit_pct",
    "0",
    "--margin_drop_factor",
    "0.04",
    "--start_ts",
    "1704067200000",
    "--end_ts",
    "1779516000000",
    "--chain-by-month",
    "--profit_target_usdt",
    "200",
]


def main() -> int:
    results = []
    for sym in SYMBOLS:
        print(f"\n{'='*60}\n>>> {sym}\n{'='*60}", flush=True)
        rc = subprocess.call([*COMMON, "--symbol", sym], env={**dict(__import__("os").environ), "PYTHONPATH": "."})
        results.append((sym, rc))
    print("\n=== Resumen ===")
    for sym, rc in results:
        print(f"  {sym}: exit {rc}")
    return 0 if all(rc == 0 for _, rc in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
