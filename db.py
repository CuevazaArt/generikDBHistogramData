"""SQLite helper for storing Binance kline (histogram) data.
"""
import sqlite3
from typing import Iterable, Tuple, Optional, List


SCHEMA = """
CREATE TABLE IF NOT EXISTS klines (
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time INTEGER NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    close_time INTEGER,
    quote_asset_volume REAL,
    num_trades INTEGER,
    taker_buy_base REAL,
    taker_buy_quote REAL,
    ignore_field TEXT,
    PRIMARY KEY (symbol, interval, open_time)
);
"""


def init_db(path: str) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.executescript(SCHEMA)
    conn.close()


def insert_klines(
    path: str, symbol: str, interval: str, rows: Iterable[Tuple]
) -> None:
    """Insert iterable of kline tuples into DB. Rows must be in the order produced by downloader.

    The downloader yields tuples of 12 fields matching the KLINE_FIELDS.
    """
    conn = sqlite3.connect(path)
    with conn:
        cur = conn.cursor()
        to_insert = []
        for r in rows:
            to_insert.append(
                (
                    symbol,
                    interval,
                    int(r[0]),
                    float(r[1]),
                    float(r[2]),
                    float(r[3]),
                    float(r[4]),
                    float(r[5]),
                    int(r[6]),
                    float(r[7]) if r[7] != "" else None,
                    int(r[8]),
                    float(r[9]) if r[9] != "" else None,
                    float(r[10]) if r[10] != "" else None,
                    str(r[11]) if len(r) > 11 else "",
                )
            )
        cur.executemany(
            """
            INSERT OR IGNORE INTO klines (
                symbol, interval, open_time, open, high, low, close, volume,
                close_time, quote_asset_volume, num_trades, taker_buy_base, taker_buy_quote, ignore_field
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            to_insert,
        )
    conn.close()


def query_klines(
    path: str,
    symbol: str,
    interval: str,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    limit: Optional[int] = None,
) -> List[Tuple]:
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    sql = "SELECT * FROM klines WHERE symbol=? AND interval=?"
    params = [symbol, interval]
    if start_ts is not None:
        sql += " AND open_time>=?"
        params.append(int(start_ts))
    if end_ts is not None:
        sql += " AND open_time<=?"
        params.append(int(end_ts))
    sql += " ORDER BY open_time ASC"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(int(limit))
    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()
    return rows


if __name__ == "__main__":
    print("DB helper: import and use init_db/insert_klines/query_klines")
