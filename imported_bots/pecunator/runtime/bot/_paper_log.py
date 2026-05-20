"""Paper-trade logger — persists simulated decisions to SQLite.

When a bot runs in ``simulated=True`` mode, its decisions are ephemeral
("print and forget").  This module logs every simulated decision to a
shared SQLite database so that paper-trading performance can be measured
retroactively without reconstructing stdout.

Schema:
    paper_trades(
        id          INTEGER PRIMARY KEY,
        bot_type    TEXT,      -- "dorothy" | "elphaba"
        bot_id      TEXT,      -- instance identifier
        symbol      TEXT,
        decision    TEXT,      -- "BUY_AND_SELL" | "WAIT" | "STOP_LOSS" | ...
        report_json TEXT,      -- full decision report as JSON
        ts_utc      TEXT       -- ISO-8601 UTC timestamp
    )
"""

from __future__ import annotations

import json
import sqlite3
import threading
import datetime as dt
from pathlib import Path
from typing import Any, Optional

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DATA_DIR / "paper_trades.sqlite"
_LOCK = threading.Lock()
_CONN: Optional[sqlite3.Connection] = None


def _get_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is None:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _CONN = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
        _CONN.execute("PRAGMA journal_mode=WAL")
        _CONN.execute("PRAGMA synchronous=NORMAL")
        _CONN.execute(
            """
            CREATE TABLE IF NOT EXISTS paper_trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_type    TEXT    NOT NULL,
                bot_id      TEXT    NOT NULL DEFAULT '',
                symbol      TEXT    NOT NULL,
                decision    TEXT    NOT NULL,
                report_json TEXT    NOT NULL,
                ts_utc      TEXT    NOT NULL
            )
            """
        )
        _CONN.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_pt_bot_symbol
            ON paper_trades(bot_type, symbol, ts_utc)
            """
        )
        _CONN.commit()
    return _CONN


def log_paper_trade(
    bot_type: str,
    symbol: str,
    decision: str,
    report: dict[str, Any],
    bot_id: str = "",
) -> None:
    """Persist a simulated trade decision.

    Safe to call from any thread.  Uses WAL mode so reads never block writes.
    """
    ts = dt.datetime.now(dt.timezone.utc).isoformat()
    report_json = json.dumps(report, default=str)
    with _LOCK:
        conn = _get_conn()
        conn.execute(
            "INSERT INTO paper_trades (bot_type, bot_id, symbol, decision, report_json, ts_utc) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (bot_type, bot_id, symbol, decision, report_json, ts),
        )
        conn.commit()


def get_paper_trades(
    bot_type: str = "",
    symbol: str = "",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Retrieve recent paper trades, newest first."""
    with _LOCK:
        conn = _get_conn()
        clauses: list[str] = []
        params: list[str] = []
        if bot_type:
            clauses.append("bot_type = ?")
            params.append(bot_type)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT id, bot_type, bot_id, symbol, decision, report_json, ts_utc "
            f"FROM paper_trades {where} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [
        {
            "id": r[0],
            "bot_type": r[1],
            "bot_id": r[2],
            "symbol": r[3],
            "decision": r[4],
            "report": json.loads(r[5]) if r[5] else {},
            "ts_utc": r[6],
        }
        for r in rows
    ]


def paper_trade_summary(bot_type: str = "", symbol: str = "") -> dict[str, Any]:
    """Quick summary: total trades, decision distribution, date range."""
    with _LOCK:
        conn = _get_conn()
        clauses: list[str] = []
        params: list[str] = []
        if bot_type:
            clauses.append("bot_type = ?")
            params.append(bot_type)
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = conn.execute(
            f"SELECT COUNT(*), MIN(ts_utc), MAX(ts_utc) FROM paper_trades {where}",
            tuple(params),
        ).fetchone()
        dist_rows = conn.execute(
            f"SELECT decision, COUNT(*) FROM paper_trades {where} GROUP BY decision",
            tuple(params),
        ).fetchall()
    return {
        "total": row[0] if row else 0,
        "first_ts": row[1] if row else None,
        "last_ts": row[2] if row else None,
        "decisions": {r[0]: r[1] for r in dist_rows},
    }
