"""Download a Binance Alpha symbol and prepare a reusable dataset artifact.

Pipeline:
  1. Resolve symbol via Alpha token list (ej. BILLUSDT -> ALPHA_953USDT).
  2. Stream klines from Alpha REST (retries + error handling).
  3. Persist to klines.db using the standard schema.
  4. Cure timestamps (cure_klines_time_format).
  5. Build a dataset artifact (Parquet cache when available, manifest.json).

El artifact queda en reports/entregables/datasets/<name>/manifest.json
para que cualquier bot futuro pueda consumir el mismo histórico sin
re-descargar.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from typing import Optional, Tuple

from binance_hist_downloader import BinanceDownloader
from db import cure_klines_time_format, init_db, insert_klines
from tqdm import tqdm  # type: ignore[import-untyped]

from backtest.dataset_artifact import prepare_dataset_artifact


ALPHA_INTERVALS = {
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h",
    "1d", "3d", "1w", "1M",
}


def _utc_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _resolve_alpha_metadata(dl: BinanceDownloader, base: str) -> dict:
    """Find token metadata in the Alpha token list (case-insensitive)."""
    tokens = dl.get_alpha_token_list()
    base_u = base.upper()
    for t in tokens:
        if str(t.get("symbol", "")).upper() == base_u:
            return t
    raise SystemExit(f"Token '{base}' not found in Alpha token list.")


def _window_in_db(db_path: str, symbol: str, interval: str) -> Tuple[Optional[int], Optional[int], int]:
    c = sqlite3.connect(db_path)
    try:
        row = c.execute(
            "SELECT MIN(open_time), MAX(open_time), COUNT(*) FROM klines WHERE symbol=? AND interval=?",
            (symbol, interval),
        ).fetchone()
    finally:
        c.close()
    return row[0], row[1], int(row[2] or 0)


def main() -> int:
    parser = argparse.ArgumentParser(description="Download + prepare an Alpha symbol dataset.")
    parser.add_argument("--symbol", required=True, help="Human symbol, ej. BILLUSDT")
    parser.add_argument("--interval", required=True, help=f"Uno de: {sorted(ALPHA_INTERVALS)}")
    parser.add_argument("--db", default="klines.db")
    parser.add_argument("--start", type=int, default=None, help="UTC ms (default: listingTime del token)")
    parser.add_argument("--end", type=int, default=None, help="UTC ms (default: ahora)")
    parser.add_argument("--batch", type=int, default=500, help="Tamaño de batch para insert SQLite")
    parser.add_argument("--output_root", default="reports", help="Base para artifacts (reports/entregables/datasets/...)")
    parser.add_argument("--artifact_name", default=None,
                        help="Nombre del artifact. Default: <symbol>_<interval>_alpha")
    parser.add_argument("--max_retries", type=int, default=6)
    parser.add_argument("--skip_download", action="store_true",
                        help="No re-descarga; solo cura datos existentes y genera artifact.")
    args = parser.parse_args()

    if args.interval not in ALPHA_INTERVALS:
        raise SystemExit(
            f"Interval '{args.interval}' no soportado por Alpha. Validos: {sorted(ALPHA_INTERVALS)}"
        )

    init_db(args.db)
    dl = BinanceDownloader()
    symbol_human = args.symbol.upper()
    base = symbol_human
    for q in ("USDT", "USDC"):
        if base.endswith(q):
            base = base[: -len(q)]
            break
    meta = _resolve_alpha_metadata(dl, base)
    alpha_symbol = f"{str(meta['alphaId']).upper()}USDT"
    listing_ms = int(meta.get("listingTime") or 0)
    start_ts = int(args.start) if args.start else (listing_ms or 0)
    end_ts = int(args.end) if args.end else int(datetime.now(tz=timezone.utc).timestamp() * 1000)

    print(
        f"[alpha-prep] {symbol_human} -> {alpha_symbol} | {meta.get('name')} | "
        f"chain={meta.get('chainName')} | liquidity={meta.get('liquidity')} | "
        f"holders={meta.get('holders')} | offline={meta.get('offline')} | "
        f"listing={_utc_iso(listing_ms) if listing_ms else 'n/a'}"
    )
    if not start_ts:
        raise SystemExit("No se pudo determinar start_ts; pasa --start explícitamente.")

    if not args.skip_download:
        print(f"[alpha-prep] descargando {symbol_human}/{args.interval} {_utc_iso(start_ts)} -> {_utc_iso(end_ts)}")
        it = dl.download_klines_alpha_api(
            alpha_symbol, args.interval,
            start_ts=start_ts, end_ts=end_ts, max_retries=int(args.max_retries),
        )
        batch = []
        total = 0
        for row in tqdm(it, desc=f"Downloading {alpha_symbol}"):
            batch.append(row)
            total += 1
            if len(batch) >= args.batch:
                insert_klines(args.db, symbol_human, args.interval, batch)
                batch = []
        if batch:
            insert_klines(args.db, symbol_human, args.interval, batch)
        print(f"[alpha-prep] descargadas {total} velas")
    else:
        print("[alpha-prep] skip_download activo; usando datos existentes.")

    fixed = cure_klines_time_format(args.db, symbol=symbol_human, interval=args.interval)
    total_fixed = int(sum(fixed.values()))
    if total_fixed:
        print(f"[alpha-prep] timestamps normalizados: {fixed}")

    min_ts, max_ts, count = _window_in_db(args.db, symbol_human, args.interval)
    if not count:
        raise SystemExit(f"No hay velas en DB tras descarga para {symbol_human}/{args.interval}.")
    print(f"[alpha-prep] DB: {count} velas, {_utc_iso(min_ts)} -> {_utc_iso(max_ts)}")

    artifact_name = args.artifact_name or f"{symbol_human.lower()}_{args.interval}_alpha"
    manifest = prepare_dataset_artifact(
        db_path=args.db,
        symbol=symbol_human,
        interval=args.interval,
        start_ts=int(min_ts),
        end_ts=int(max_ts),
        output_base=args.output_root,
        artifact_name=artifact_name,
        prefer_parquet_cache=True,
    )
    # Anotamos metadata del token Alpha al final del manifest para trazabilidad.
    enriched_path = manifest.get("source", {}).get("db_path", args.db).rsplit("\\", 1)[0] if "\\" in str(manifest.get("source", {}).get("db_path", "")) else None
    print(
        f"[alpha-prep] artifact: name={manifest['artifact_name']} "
        f"rows={manifest['integrity']['row_count']} gaps={manifest['integrity']['gap_count']} "
        f"parquet_cache={manifest['notes']['parquet_cache_used']}"
    )
    if manifest["integrity"]["has_gaps"]:
        print(
            f"[alpha-prep] WARNING: {manifest['integrity']['gap_count']} gaps detectados; "
            f"primeros: {manifest['integrity']['gaps'][:3]}"
        )
    # Persistimos metadata Alpha junto al manifest para que otros bots/cargadores la usen.
    extra_path = manifest.get("source", {}).get("db_path")
    artifact_dir = None
    try:
        # El artifact dir es <output_root>/entregables/datasets/<artifact_name>
        from backtest.report_paths import dataset_report_dir
        artifact_dir = dataset_report_dir(args.output_root, artifact_name, ensure_manifest=False)
        alpha_meta_path = f"{artifact_dir}/alpha_token.json"
        with open(alpha_meta_path, "w", encoding="utf-8") as fh:
            json.dump({
                "human_symbol": symbol_human,
                "alpha_symbol": alpha_symbol,
                "interval": args.interval,
                "token_metadata": meta,
                "downloaded_at": datetime.now(tz=timezone.utc).isoformat(),
            }, fh, ensure_ascii=False, indent=2, default=str)
        print(f"[alpha-prep] metadata Alpha guardada: {alpha_meta_path}")
    except Exception as exc:
        print(f"[alpha-prep] no se pudo escribir alpha_token.json: {exc}", file=sys.stderr)

    print(f"[alpha-prep] done. Artifact en: {artifact_dir or 'reports/entregables/datasets/' + artifact_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
