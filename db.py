"""SQLite helpers for market data and backtesting artifacts."""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


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

BACKTEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS bt_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_name TEXT NOT NULL,
    symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    start_ts INTEGER,
    end_ts INTEGER,
    initial_cash REAL NOT NULL,
    fee_rate REAL NOT NULL,
    slippage_bps REAL NOT NULL,
    config_json TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    created_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS bt_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    trial_id INTEGER,
    seq INTEGER NOT NULL,
    event_time INTEGER,
    event_type TEXT NOT NULL,
    side TEXT,
    price REAL,
    qty REAL,
    cash REAL,
    equity REAL,
    position_qty REAL,
    payload_json TEXT,
    FOREIGN KEY (run_id) REFERENCES bt_runs (run_id)
);

CREATE TABLE IF NOT EXISTS bt_metrics (
    metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    trial_id INTEGER,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    extra_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, trial_id, metric_name),
    FOREIGN KEY (run_id) REFERENCES bt_runs (run_id)
);

CREATE TABLE IF NOT EXISTS bt_trials (
    trial_id INTEGER PRIMARY KEY AUTOINCREMENT,
    study_name TEXT NOT NULL,
    trial_number INTEGER NOT NULL,
    state TEXT NOT NULL,
    objective REAL,
    params_json TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_sec REAL
);

CREATE TABLE IF NOT EXISTS bt_trial_metrics (
    trial_metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id INTEGER NOT NULL,
    metric_name TEXT NOT NULL,
    metric_value REAL NOT NULL,
    UNIQUE(trial_id, metric_name),
    FOREIGN KEY (trial_id) REFERENCES bt_trials (trial_id)
);

CREATE INDEX IF NOT EXISTS idx_bt_events_run_seq ON bt_events(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_bt_events_run_time ON bt_events(run_id, event_time);
CREATE INDEX IF NOT EXISTS idx_bt_events_trial ON bt_events(trial_id);
CREATE INDEX IF NOT EXISTS idx_bt_metrics_run ON bt_metrics(run_id);
CREATE INDEX IF NOT EXISTS idx_bt_trials_study_obj ON bt_trials(study_name, objective);
CREATE INDEX IF NOT EXISTS idx_bt_trial_metrics_trial ON bt_trial_metrics(trial_id);
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(path: str) -> None:
    conn = _connect(path)
    with conn:
        conn.executescript(SCHEMA + "\n" + BACKTEST_SCHEMA)
    conn.close()


def insert_klines(
    path: str, symbol: str, interval: str, rows: Iterable[Tuple]
) -> None:
    """Insert iterable of kline tuples into DB. Rows must be in the order produced by downloader.

    The downloader yields tuples of 12 fields matching the KLINE_FIELDS.
    """
    conn = _connect(path)
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
    conn = _connect(path)
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


def create_bt_run(
    path: str,
    strategy_name: str,
    symbol: str,
    interval: str,
    start_ts: Optional[int],
    end_ts: Optional[int],
    initial_cash: float,
    fee_rate: float,
    slippage_bps: float,
    config: Optional[Dict[str, Any]] = None,
) -> int:
    conn = _connect(path)
    with conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO bt_runs (
                strategy_name, symbol, interval, start_ts, end_ts, initial_cash,
                fee_rate, slippage_bps, config_json, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?)
            """,
            (
                strategy_name,
                symbol,
                interval,
                int(start_ts) if start_ts is not None else None,
                int(end_ts) if end_ts is not None else None,
                float(initial_cash),
                float(fee_rate),
                float(slippage_bps),
                json.dumps(config or {}, ensure_ascii=False),
                _utc_now(),
            ),
        )
        run_id = int(cur.lastrowid)
    conn.close()
    return run_id


def finish_bt_run(path: str, run_id: int, status: str = "completed") -> None:
    conn = _connect(path)
    with conn:
        conn.execute(
            "UPDATE bt_runs SET status=?, ended_at=? WHERE run_id=?",
            (status, _utc_now(), int(run_id)),
        )
    conn.close()


def insert_bt_events(path: str, run_id: int, events: Iterable[Dict[str, Any]]) -> None:
    rows = []
    for e in events:
        rows.append(
            (
                int(run_id),
                int(e.get("trial_id")) if e.get("trial_id") is not None else None,
                int(e["seq"]),
                int(e["event_time"]) if e.get("event_time") is not None else None,
                str(e["event_type"]),
                e.get("side"),
                float(e["price"]) if e.get("price") is not None else None,
                float(e["qty"]) if e.get("qty") is not None else None,
                float(e["cash"]) if e.get("cash") is not None else None,
                float(e["equity"]) if e.get("equity") is not None else None,
                float(e["position_qty"]) if e.get("position_qty") is not None else None,
                json.dumps(e.get("payload", {}), ensure_ascii=False),
            )
        )
    if not rows:
        return
    conn = _connect(path)
    with conn:
        conn.executemany(
            """
            INSERT INTO bt_events (
                run_id, trial_id, seq, event_time, event_type, side, price, qty, cash,
                equity, position_qty, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.close()


def upsert_bt_metrics(
    path: str,
    run_id: int,
    metrics: Dict[str, float],
    trial_id: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    now = _utc_now()
    extra_json = json.dumps(extra or {}, ensure_ascii=False)
    rows = [
        (
            int(run_id),
            int(trial_id) if trial_id is not None else None,
            name,
            float(value),
            extra_json,
            now,
        )
        for name, value in metrics.items()
    ]
    if not rows:
        return
    conn = _connect(path)
    with conn:
        conn.executemany(
            """
            INSERT INTO bt_metrics (run_id, trial_id, metric_name, metric_value, extra_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, trial_id, metric_name) DO UPDATE SET
                metric_value=excluded.metric_value,
                extra_json=excluded.extra_json,
                created_at=excluded.created_at
            """,
            rows,
        )
    conn.close()


def create_bt_trial(
    path: str,
    study_name: str,
    trial_number: int,
    state: str,
    objective: Optional[float],
    params: Dict[str, Any],
    started_at: str,
    finished_at: Optional[str],
    duration_sec: Optional[float],
) -> int:
    conn = _connect(path)
    with conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO bt_trials (
                study_name, trial_number, state, objective, params_json,
                started_at, finished_at, duration_sec
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                study_name,
                int(trial_number),
                state,
                float(objective) if objective is not None else None,
                json.dumps(params, ensure_ascii=False),
                started_at,
                finished_at,
                float(duration_sec) if duration_sec is not None else None,
            ),
        )
        trial_id = int(cur.lastrowid)
    conn.close()
    return trial_id


def upsert_bt_trial_metrics(path: str, trial_id: int, metrics: Dict[str, float]) -> None:
    rows = [(int(trial_id), name, float(value)) for name, value in metrics.items()]
    if not rows:
        return
    conn = _connect(path)
    with conn:
        conn.executemany(
            """
            INSERT INTO bt_trial_metrics (trial_id, metric_name, metric_value)
            VALUES (?, ?, ?)
            ON CONFLICT(trial_id, metric_name) DO UPDATE SET
                metric_value=excluded.metric_value
            """,
            rows,
        )
    conn.close()


def list_bt_runs(path: str, limit: int = 20) -> List[Tuple]:
    conn = _connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT run_id, strategy_name, symbol, interval, status, created_at, ended_at
        FROM bt_runs
        ORDER BY run_id DESC
        LIMIT ?
        """,
        (int(limit),),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_bt_run_metrics(path: str, run_id: int) -> Dict[str, float]:
    conn = _connect(path)
    cur = conn.cursor()
    cur.execute(
        "SELECT metric_name, metric_value FROM bt_metrics WHERE run_id=?",
        (int(run_id),),
    )
    rows = cur.fetchall()
    conn.close()
    return {r[0]: float(r[1]) for r in rows}


def get_bt_recent_events(path: str, run_id: int, limit: int = 30) -> List[Tuple]:
    conn = _connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT seq, event_time, event_type, side, price, qty, cash, equity, position_qty, payload_json
        FROM bt_events
        WHERE run_id=?
        ORDER BY seq DESC
        LIMIT ?
        """,
        (int(run_id), int(limit)),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def list_top_bt_trials(path: str, study_name: str, limit: int = 10) -> List[Tuple]:
    conn = _connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT trial_id, trial_number, state, objective, params_json, started_at, finished_at
        FROM bt_trials
        WHERE study_name=?
        ORDER BY objective DESC
        LIMIT ?
        """,
        (study_name, int(limit)),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_bt_equity_curve(path: str, run_id: int) -> List[Tuple]:
    conn = _connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT seq, event_time, equity
        FROM bt_events
        WHERE run_id=? AND equity IS NOT NULL
        ORDER BY seq ASC
        """,
        (int(run_id),),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_bt_trial_objectives(path: str, study_name: str, limit: int = 500) -> List[Tuple]:
    conn = _connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT trial_number, objective
        FROM bt_trials
        WHERE study_name=? AND objective IS NOT NULL
        ORDER BY trial_number ASC
        LIMIT ?
        """,
        (study_name, int(limit)),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


if __name__ == "__main__":
    print("DB helper: import and use init_db/insert_klines/query_klines/backtesting helpers")
