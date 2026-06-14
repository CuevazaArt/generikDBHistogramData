"""Download Binance monthly klines zip files into the partitioned Parquet lake.

Layout produced (matches `backtest.storage_paths.StoragePaths.klines_partition`):

    data/klines/symbol=<SYM>/interval=<INT>/year=<YYYY>/month=<MM>/part-000.parquet
    data/klines/symbol=<SYM>/interval=<INT>/year=<YYYY>/month=<MM>/_manifest.json

Idempotent and resumable: months that already have a `part-000.parquet` are
skipped unless `--overwrite` is set. Network/parse failures for a single month
are logged and the run keeps going.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover - hard requirement
    raise SystemExit(
        "pyarrow is required for fetch_klines_to_parquet. Run: pip install pyarrow"
    ) from exc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from binance_hist_downloader import BinanceDownloader
from backtest.storage_paths import StoragePaths, tmp_then_rename


FETCHER_VERSION = "0.1.0"
ROW_GROUP_SIZE = 65_536
WRITE_BATCH_ROWS = 65_536

# Column order matches `scripts/backup_klines_to_parquet.py` so DuckDB and
# pyarrow can read partitions written by either tool with one schema.
COLUMNS: List[Tuple[str, "pa.DataType"]] = [
    ("open_time", pa.int64()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.float64()),
    ("close_time", pa.int64()),
    ("quote_asset_volume", pa.float64()),
    ("num_trades", pa.int64()),
    ("taker_buy_base", pa.float64()),
    ("taker_buy_quote", pa.float64()),
    ("ignore_field", pa.string()),
]
SCHEMA = pa.schema(COLUMNS)


def _empty_batch() -> dict:
    return {name: [] for name, _ in COLUMNS}


def _flush_batch(writer: pq.ParquetWriter, batch: dict) -> int:
    if not batch["open_time"]:
        return 0
    table = pa.Table.from_pydict(batch, schema=SCHEMA)
    writer.write_table(table, row_group_size=ROW_GROUP_SIZE)
    return table.num_rows


def _row_into_batch(batch: dict, row: Tuple) -> None:
    # Tuple shape from `BinanceDownloader.download_klines_zip` is documented
    # in that module: (open_time, o, h, l, c, v, close_time, qav, n_trades,
    # taker_buy_base, taker_buy_quote, ignore_field).
    batch["open_time"].append(int(row[0]))
    batch["open"].append(float(row[1]))
    batch["high"].append(float(row[2]))
    batch["low"].append(float(row[3]))
    batch["close"].append(float(row[4]))
    batch["volume"].append(float(row[5]))
    batch["close_time"].append(int(row[6]))
    batch["quote_asset_volume"].append(float(row[7]) if row[7] is not None else 0.0)
    batch["num_trades"].append(int(row[8]) if row[8] is not None else 0)
    batch["taker_buy_base"].append(float(row[9]) if row[9] is not None else 0.0)
    batch["taker_buy_quote"].append(float(row[10]) if row[10] is not None else 0.0)
    batch["ignore_field"].append(str(row[11]) if row[11] is not None else "")


def _write_manifest(
    target_dir: str,
    *,
    symbol: str,
    interval: str,
    year: int,
    month: int,
    row_count: int,
    min_open_time: int | None,
    max_open_time: int | None,
) -> str:
    manifest_path = os.path.join(target_dir, "_manifest.json")
    payload = {
        "symbol": symbol,
        "interval": interval,
        "year": int(year),
        "month": int(month),
        "row_count": int(row_count),
        "min_open_time": int(min_open_time) if min_open_time is not None else None,
        "max_open_time": int(max_open_time) if max_open_time is not None else None,
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "source": "binance.vision.zip",
        "fetcher_version": FETCHER_VERSION,
    }
    with tmp_then_rename(manifest_path) as tmp:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    return manifest_path


def _fetch_one_month(
    *,
    downloader: BinanceDownloader,
    paths: StoragePaths,
    symbol: str,
    interval: str,
    year: int,
    month: int,
    overwrite: bool,
    max_retries: int,
) -> Tuple[str, int, str]:
    """Return (status, rows_written, message). Status is 'ok'|'skip'|'fail'."""
    target = paths.klines_partition(symbol, interval, year, month)
    target_dir = os.path.dirname(target)
    if os.path.exists(target) and not overwrite:
        return ("skip", 0, f"{target} already exists")

    os.makedirs(target_dir, exist_ok=True)

    rows_written = 0
    min_open_time: int | None = None
    max_open_time: int | None = None
    batch = _empty_batch()
    try:
        with tmp_then_rename(target) as tmp_path:
            writer = pq.ParquetWriter(tmp_path, SCHEMA, compression="snappy")
            try:
                for row in downloader.download_klines_zip(
                    symbol=symbol,
                    interval=interval,
                    year=year,
                    month=month,
                    max_retries=max_retries,
                ):
                    open_time = int(row[0])
                    if min_open_time is None or open_time < min_open_time:
                        min_open_time = open_time
                    if max_open_time is None or open_time > max_open_time:
                        max_open_time = open_time
                    _row_into_batch(batch, row)
                    if len(batch["open_time"]) >= WRITE_BATCH_ROWS:
                        rows_written += _flush_batch(writer, batch)
                        batch = _empty_batch()
                rows_written += _flush_batch(writer, batch)
            finally:
                writer.close()
    except Exception as exc:
        return ("fail", 0, f"{type(exc).__name__}: {exc}")

    if rows_written == 0:
        try:
            os.remove(target)
        except OSError:
            pass
        return ("fail", 0, "zip produced zero rows")

    _write_manifest(
        target_dir,
        symbol=symbol,
        interval=interval,
        year=year,
        month=month,
        row_count=rows_written,
        min_open_time=min_open_time,
        max_open_time=max_open_time,
    )
    return ("ok", rows_written, target)


def _months_to_fetch(month_arg: int | None) -> Iterable[int]:
    if month_arg is None:
        return range(1, 13)
    if not (1 <= int(month_arg) <= 12):
        raise SystemExit(f"--month must be in [1, 12], got {month_arg}")
    return (int(month_arg),)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--interval", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument(
        "--month",
        type=int,
        default=None,
        help="If omitted, fetches months 1..12 of --year.",
    )
    parser.add_argument("--data-root", default="data")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-download a month even if part-000.parquet already exists.",
    )
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument(
        "--sleep-between-months",
        type=float,
        default=2.0,
        help="Polite delay between successful month downloads (seconds).",
    )
    args = parser.parse_args()

    paths = StoragePaths(data_root=str(args.data_root))
    os.makedirs(paths.klines_root(), exist_ok=True)
    downloader = BinanceDownloader()

    months = list(_months_to_fetch(args.month))
    started = time.monotonic()
    total = len(months)
    success = 0
    skipped = 0
    failed = 0
    total_rows = 0

    for idx, month in enumerate(months):
        tag = f"{args.symbol} {args.interval} {args.year}-{month:02d}"
        print(f"[fetch] {tag} -> starting", flush=True)
        status, rows, message = _fetch_one_month(
            downloader=downloader,
            paths=paths,
            symbol=str(args.symbol).upper(),
            interval=str(args.interval),
            year=int(args.year),
            month=int(month),
            overwrite=bool(args.overwrite),
            max_retries=int(args.max_retries),
        )
        if status == "ok":
            success += 1
            total_rows += rows
            print(f"[ok]   {tag} rows={rows} path={message}", flush=True)
        elif status == "skip":
            skipped += 1
            print(f"[skip] {tag} {message}", flush=True)
        else:
            failed += 1
            print(f"[fail] {tag} {message}", flush=True, file=sys.stderr)

        if status == "ok" and idx < len(months) - 1 and args.sleep_between_months > 0:
            time.sleep(float(args.sleep_between_months))

    duration = time.monotonic() - started
    print(
        f"total_months={total} success={success} skipped={skipped} "
        f"failed={failed} duration={duration:.1f}s total_rows={total_rows}",
        flush=True,
    )
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
