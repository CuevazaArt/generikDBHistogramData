"""Build a tiny synthetic klines SQLite DB for CI nightly integrity checks.

Generates ~1000 BTCUSDT 1h candles starting at a deterministic epoch so the
backup/verify/purge pipeline can be exercised end-to-end without depending on
any real market data being present in the runner.
"""

from __future__ import annotations

import argparse
import math
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db


def _row(open_time_ms: int, idx: int) -> tuple:
    base = 50_000.0 + 200.0 * math.sin(idx / 17.0)
    open_p = base
    close_p = base + 25.0 * math.cos(idx / 11.0)
    high_p = max(open_p, close_p) + 15.0
    low_p = min(open_p, close_p) - 15.0
    volume = 10.0 + abs(math.sin(idx / 7.0)) * 5.0
    close_time_ms = open_time_ms + 3_600_000 - 1
    quote_vol = volume * ((open_p + close_p) / 2.0)
    num_trades = 100 + idx % 50
    taker_base = volume * 0.55
    taker_quote = quote_vol * 0.55
    return (
        open_time_ms,
        open_p,
        high_p,
        low_p,
        close_p,
        volume,
        close_time_ms,
        quote_vol,
        num_trades,
        taker_base,
        taker_quote,
        "0",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="Path of the SQLite file to create")
    parser.add_argument("--symbol", default="BTCUSDT")
    parser.add_argument("--interval", default="1h")
    parser.add_argument("--rows", type=int, default=1000)
    parser.add_argument(
        "--start-ms",
        type=int,
        default=1_704_067_200_000,
        help="Open time of first candle in ms (default: 2024-01-01T00:00:00Z)",
    )
    args = parser.parse_args()

    if args.rows <= 0:
        raise SystemExit("--rows must be > 0")

    parent = os.path.dirname(os.path.abspath(args.db))
    if parent:
        os.makedirs(parent, exist_ok=True)

    rows = [_row(args.start_ms + i * 3_600_000, i) for i in range(args.rows)]

    db.init_db(args.db)
    db.insert_klines(args.db, args.symbol, args.interval, rows)

    print(f"built synthetic klines DB: {args.db} ({args.rows} {args.symbol}/{args.interval} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
