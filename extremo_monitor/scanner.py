"""Multi-symbol scanner that feeds the detector with klines from the DB.

Usage (standalone)::

    python -m extremo_monitor.scanner [--db klines.db] [--top N]

Reads daily klines from *klines.db* for every USDT pair found, runs the
extreme detector, and prints a table of actionable symbols sorted by score.
"""

from __future__ import annotations

import argparse
import os
import sys
import sqlite3
from typing import Dict, List, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from extremo_monitor.config import MonitorConfig
from extremo_monitor.detector import evaluate_extreme, passes_survival_filter, ExtremeResult


# ---------------------------------------------------------------------------
# DB helpers (thin wrappers around db.py conventions)
# ---------------------------------------------------------------------------

def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA cache_size=-32768")
    return conn


def _best_interval(db_path: str, symbol: str) -> str | None:
    """Return the best available interval for a symbol.

    Preference order: ``1d > 1h > 15m > 1m > 1s``.
    For ``1s``/``1m`` we aggregate in SQL to avoid loading millions of rows.
    """
    conn = _connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT DISTINCT interval FROM klines WHERE symbol = ?",
        (symbol,),
    )
    available = {r[0] for r in cur.fetchall()}
    conn.close()
    for pref in ("1d", "1h", "15m", "1m", "1s"):
        if pref in available:
            return pref
    return None


def list_usdt_symbols(db_path: str, quote: str = "USDT") -> List[str]:
    """Return all symbols ending with *quote* that have klines in any interval."""
    conn = _connect(db_path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT symbol FROM klines
        WHERE symbol LIKE ?
        ORDER BY symbol
        """,
        (f"%{quote}",),
    )
    symbols = [row[0] for row in cur.fetchall()]
    conn.close()
    return symbols


def fetch_daily_klines(
    db_path: str, symbol: str
) -> Tuple[List[float], List[int], List[float]]:
    """Fetch daily closes, timestamps, and volumes for *symbol*.

    If native ``1d`` klines are not available, aggregates from smaller
    intervals using SQL ``GROUP BY`` to keep memory bounded.

    Returns ``(closes, timestamps, volumes)`` ordered chronologically.
    """
    interval = _best_interval(db_path, symbol)
    if interval is None:
        return [], [], []

    conn = _connect(db_path)
    cur = conn.cursor()

    if interval == "1d":
        cur.execute(
            """
            SELECT open_time, close, volume
            FROM klines
            WHERE symbol = ? AND interval = '1d'
            ORDER BY open_time ASC
            """,
            (symbol,),
        )
        rows = cur.fetchall()
        conn.close()
        return (
            [float(r[1]) for r in rows],
            [int(r[0]) for r in rows],
            [float(r[2]) for r in rows],
        )

    # For sub-daily intervals: aggregate to daily in SQL.
    # We use a two-step approach:
    # 1. Get the close from the last candle per day (via MAX open_time)
    # 2. Get the total volume per day
    # This avoids correlated subqueries which are slow on 75M-row tables.
    DAY_MS = 86_400_000
    cur.execute(
        f"""
        SELECT
            day_bucket,
            -- Get the close of the last candle in each day
            (SELECT k2.close FROM klines k2
             WHERE k2.symbol = ? AND k2.interval = ?
               AND k2.open_time = max_ot
             LIMIT 1) AS day_close,
            day_volume
        FROM (
            SELECT
                (open_time / {DAY_MS}) * {DAY_MS} AS day_bucket,
                MAX(open_time) AS max_ot,
                SUM(volume) AS day_volume
            FROM klines
            WHERE symbol = ? AND interval = ?
            GROUP BY (open_time / {DAY_MS})
        )
        ORDER BY day_bucket ASC
        """,
        (symbol, interval, symbol, interval),
    )
    rows = cur.fetchall()
    conn.close()

    return (
        [float(r[1]) for r in rows],
        [int(r[0]) for r in rows],
        [float(r[2]) for r in rows],
    )


def estimate_listing_days(timestamps: List[int]) -> int:
    """Rough estimate of how many days the asset has been listed."""
    if len(timestamps) < 2:
        return 0
    return max(0, (timestamps[-1] - timestamps[0]) // 86_400_000)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def scan_all(
    config: MonitorConfig | None = None,
    db_path: str | None = None,
    symbols: List[str] | None = None,
    verbose: bool = True,
) -> List[ExtremeResult]:
    """Scan USDT symbols in the klines DB and return extreme results.

    If *symbols* is provided, only those symbols are scanned (skips the
    potentially slow ``SELECT DISTINCT`` on large databases).
    """
    if config is None:
        config = MonitorConfig()
    if db_path is None:
        db_path = config.klines_db_path

    if symbols is None:
        if verbose:
            print("  Listando símbolos disponibles...")
        symbols = list_usdt_symbols(db_path, quote=config.quote_asset)

    if verbose:
        print(f"  {len(symbols)} símbolos a evaluar")

    results: List[ExtremeResult] = []
    skipped = 0

    for i, sym in enumerate(symbols):
        if verbose and (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(symbols)}] procesando {sym}...")

        closes, timestamps, volumes = fetch_daily_klines(db_path, sym)
        if len(closes) < 30:
            skipped += 1
            if verbose:
                print(f"    {sym}: saltado (solo {len(closes)} dias de datos)")
            continue

        # Survival filter — volume is in base asset, multiply by close for USD
        listing_days = estimate_listing_days(timestamps)
        vol_24h_usd = (volumes[-1] * closes[-1]) if volumes and closes else 0.0
        ok, reason = passes_survival_filter(
            sym, vol_24h_usd, listing_days, config.survival
        )
        if not ok:
            skipped += 1
            if verbose:
                print(f"    {sym}: filtrado ({reason})")
            continue

        result = evaluate_extreme(
            symbol=sym,
            daily_closes=closes,
            daily_timestamps=timestamps,
            daily_volumes=volumes,
            thresholds=config.thresholds,
        )
        results.append(result)

    if verbose:
        print(f"  Evaluados: {len(results)} | Saltados: {skipped}")

    # Sort by score descending (most extreme first)
    results.sort(key=lambda r: (-r.score, -r.confluence, r.symbol))
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_table(results: List[ExtremeResult], top: int = 30) -> None:
    """Pretty-print a summary table of extreme results."""
    shown = results[:top]
    if not shown:
        print("\nNo se encontraron símbolos con datos suficientes.")
        return

    hdr = f"{'Simbolo':<14} {'Score':>6} {'Conf':>5} {'Accion':>7}  {'Precio':>12} {'ATH':>12} {'Drawdown':>9}  Senales"
    sep = "-" * len(hdr)
    print(f"\n{hdr}")
    print(sep)

    for r in shown:
        dd_signal = next((s for s in r.signals if s.name == "drawdown_ath"), None)
        dd_str = f"{dd_signal.value:.1%}" if dd_signal else "-"
        flags = " ".join(
            ("[X]" if s.active else "[ ]") for s in r.signals
        )
        action = ">> SI" if r.actionable else "    -"
        print(
            f"{r.symbol:<14} {r.score:>6.0%} {r.confluence:>5}/5 {action}  "
            f"${r.current_price:>11,.4f} ${r.ath:>11,.4f} {dd_str:>9}  {flags}"
        )

    # Legend
    actionable_count = sum(1 for r in results if r.actionable)
    print(sep)
    print(
        f"Total escaneados: {len(results)} | "
        f"Accionables (>=3 confluencia): {actionable_count}"
    )
    print("Senales: [X] activa  [ ] inactiva  [Drawdown | RSI | MA200 | Volumen | Percentil]")


# Well-known symbols for quick scans when DISTINCT is too slow on large DBs
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "SOLUSDT",
    "DOTUSDT", "DOGEUSDT", "AVAXUSDT", "LINKUSDT", "MATICUSDT", "UNIUSDT",
    "LTCUSDT", "ATOMUSDT", "ETCUSDT", "XLMUSDT", "FILUSDT", "TRXUSDT",
    "NEARUSDT", "APTUSDT", "AAVEUSDT", "ALGOUSDT", "FTMUSDT", "SANDUSDT",
    "MANAUSDT", "AXSUSDT", "GALAUSDT", "THETAUSDT", "VETUSDT", "ICPUSDT",
    "RUNEUSDT", "INJUSDT", "SUIUSDT", "SEIUSDT", "TIAUSDT", "OPUSDT",
    "ARBUSDT", "LDOUSDT", "MKRUSDT", "GRTUSDT", "IMXUSDT", "APEUSDT",
    "PEPEUSDT", "SHIBUSDT", "BONKUSDT", "WIFUSDT", "FLOKIUSDT",
    "RENDERUSDT", "FETUSDT", "TAOUSDT",
]


def main() -> None:
    parser = argparse.ArgumentParser(description="Extremo Monitor — Scanner")
    parser.add_argument("--db", default="klines.db", help="Path to klines.db")
    parser.add_argument("--top", type=int, default=30, help="Show top N results")
    parser.add_argument(
        "--symbols", nargs="*", default=None,
        help="Specific symbols to scan (default: top-50 well-known pairs)"
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Scan ALL USDT symbols in DB (slow on large DBs)"
    )
    args = parser.parse_args()

    if not os.path.isfile(args.db):
        print(f"Error: {args.db} no encontrado.", file=sys.stderr)
        sys.exit(1)

    config = MonitorConfig(klines_db_path=args.db)

    if args.symbols:
        symbols = [s.upper() for s in args.symbols]
    elif args.all:
        symbols = None  # will trigger DISTINCT query
    else:
        symbols = DEFAULT_SYMBOLS

    print(f"Extremo Monitor — Escaneando en {args.db}")
    results = scan_all(config, db_path=args.db, symbols=symbols)
    _print_table(results, top=args.top)


if __name__ == "__main__":
    main()

