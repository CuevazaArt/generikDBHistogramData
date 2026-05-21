"""CLI to download Binance histogram (kline) data and store it in an SQLite DB.

Usage examples are in README_BINANCE.md
"""
import argparse
from datetime import datetime
from binance_hist_downloader import BinanceDownloader
from db import cure_klines_time_format, init_db, insert_klines
from dateutil import parser as dateparser  # type: ignore[import-untyped]
from tqdm import tqdm  # type: ignore[import-untyped]


def parse_time(s: str):
    # Accept integer milliseconds, seconds, or ISO dates
    try:
        v = int(s)
        # Heuristic: if looks like seconds (10 digits) -> convert to ms
        if v < 1e11:
            return int(v * 1000)
        return int(v)
    except Exception:
        dt = dateparser.parse(s)
        return int(dt.timestamp() * 1000)


def main():
    p = argparse.ArgumentParser(description="Descargar klines y guardarlos en sqlite")
    p.add_argument("--mode", choices=("api", "alpha_api", "zip"), default="api")
    p.add_argument("--symbol", required=True)
    p.add_argument("--interval", required=True)
    p.add_argument("--db", default="klines.db")
    p.add_argument("--start")
    p.add_argument("--end")
    p.add_argument("--year", type=int)
    p.add_argument("--month", type=int)
    p.add_argument("--batch", type=int, default=5000, help="Batch size for inserts")
    args = p.parse_args()

    init_db(args.db)
    dl = BinanceDownloader()

    if args.mode == "api":
        start_ts = parse_time(args.start) if args.start else None
        end_ts = parse_time(args.end) if args.end else None
        it = dl.download_klines_api(args.symbol, args.interval, start_ts=start_ts, end_ts=end_ts)
        batch = []
        for row in tqdm(it, desc="Downloading"):
            batch.append(row)
            if len(batch) >= args.batch:
                insert_klines(args.db, args.symbol, args.interval, batch)
                batch = []
        if batch:
            insert_klines(args.db, args.symbol, args.interval, batch)
    elif args.mode == "alpha_api":
        start_ts = parse_time(args.start) if args.start else None
        end_ts = parse_time(args.end) if args.end else None
        try:
            alpha_symbol = dl.resolve_alpha_symbol(args.symbol)
        except Exception as exc:
            raise SystemExit(f"[alpha_api] failed resolving symbol '{args.symbol}': {exc}") from exc
        print(f"[alpha_api] resolved {args.symbol.upper()} -> {alpha_symbol}")
        try:
            it = dl.download_klines_alpha_api(alpha_symbol, args.interval, start_ts=start_ts, end_ts=end_ts)
            batch = []
            for row in tqdm(it, desc="Downloading alpha"):
                batch.append(row)
                if len(batch) >= args.batch:
                    insert_klines(args.db, args.symbol, args.interval, batch)
                    batch = []
            if batch:
                insert_klines(args.db, args.symbol, args.interval, batch)
        except Exception as exc:
            raise SystemExit(
                f"[alpha_api] failed downloading klines for resolved symbol '{alpha_symbol}': {exc}"
            ) from exc
    else:
        if not args.year or not args.month:
            raise SystemExit("--year and --month required for zip mode")
        it = dl.download_klines_zip(args.symbol, args.interval, args.year, args.month)
        batch = []
        for row in tqdm(it, desc="Importing zip"):
            batch.append(row)
            if len(batch) >= args.batch:
                insert_klines(args.db, args.symbol, args.interval, batch)
                batch = []
        if batch:
            insert_klines(args.db, args.symbol, args.interval, batch)

    # Safety net: normalize timestamps in case source format differs.
    fixed = cure_klines_time_format(args.db, symbol=args.symbol, interval=args.interval)
    total_fixed = int(sum(fixed.values()))
    if total_fixed > 0:
        print(f"[cure] normalized timestamp rows: {fixed}")
    else:
        print("[cure] no timestamp normalization needed.")


if __name__ == "__main__":
    main()
