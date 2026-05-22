"""SQLite helpers for market data and backtesting artifacts."""
import json
import sqlite3
import time
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
    """Open SQLite with PRAGMAs tuned for heavy parallel write workloads.

    - WAL journaling lets many readers coexist with one writer per file.
    - `synchronous=NORMAL` is safe under WAL and ~2-5x faster than FULL.
    - `temp_store=MEMORY` and a larger cache reduce IO during big batches.
    - `mmap_size` lets the OS page-cache assist large sequential scans.
    """
    conn = sqlite3.connect(path, timeout=60.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")  # ~64 MB negative = KB units
    conn.execute("PRAGMA mmap_size=268435456")  # 256 MB if supported
    return conn


def _run_db_write_with_retry(fn: Any, retries: int = 6, base_delay_sec: float = 0.2) -> Any:
    """Retry transient sqlite lock errors with exponential backoff."""
    attempt = 0
    while True:
        try:
            return fn()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "database is locked" not in msg and "database table is locked" not in msg:
                raise
            if attempt >= retries:
                raise
            time.sleep(base_delay_sec * (2**attempt))
            attempt += 1


def init_db(path: str) -> None:
    conn = _connect(path)
    with conn:
        conn.executescript(SCHEMA + "\n" + BACKTEST_SCHEMA)
    conn.close()


def normalize_epoch_ms(value: Any) -> int:
    """Normalize epoch timestamps to milliseconds.

    Accepts values in seconds, milliseconds, microseconds or nanoseconds.
    """
    v = int(float(value))
    av = abs(v)
    if av >= 100_000_000_000_000_000:  # nanoseconds
        return int(v // 1_000_000)
    if av >= 10_000_000_000_000:  # microseconds
        return int(v // 1_000)
    if av < 100_000_000_000:  # seconds
        return int(v * 1_000)
    return int(v)


def cure_kline_row_format(row: Tuple) -> Tuple:
    """Normalize one kline row to the expected internal format."""
    if len(row) < 11:
        raise ValueError("Kline row must have at least 11 fields")
    open_time = normalize_epoch_ms(row[0])
    close_time = normalize_epoch_ms(row[6]) if row[6] is not None else None
    return (
        open_time,
        float(row[1]),
        float(row[2]),
        float(row[3]),
        float(row[4]),
        float(row[5]),
        close_time,
        float(row[7]) if row[7] not in ("", None) else None,
        int(row[8]),
        float(row[9]) if row[9] not in ("", None) else None,
        float(row[10]) if row[10] not in ("", None) else None,
        str(row[11]) if len(row) > 11 else "",
    )


def cure_klines_time_format(path: str, symbol: Optional[str] = None, interval: Optional[str] = None) -> Dict[str, int]:
    """Detect and normalize stored kline timestamps to milliseconds.

    Repairs rows persisted in seconds/microseconds/nanoseconds.
    Returns number of updated rows by source format.
    """
    conn = _connect(path)
    where = []
    params: List[Any] = []
    if symbol:
        where.append("symbol = ?")
        params.append(symbol)
    if interval:
        where.append("interval = ?")
        params.append(interval)
    where_sql = (" AND " + " AND ".join(where)) if where else ""

    sec_count = 0
    us_count = 0
    ns_count = 0
    with conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM klines WHERE open_time > 0 AND open_time < 100000000000{where_sql}",
            tuple(params),
        )
        sec_count = int(cur.fetchone()[0] or 0)
        if sec_count:
            cur.execute(
                f"""
                UPDATE klines
                SET
                    open_time = open_time * 1000,
                    close_time = CASE WHEN close_time IS NULL THEN NULL ELSE close_time * 1000 END
                WHERE open_time > 0 AND open_time < 100000000000{where_sql}
                """,
                tuple(params),
            )

        cur.execute(
            f"SELECT COUNT(*) FROM klines WHERE open_time >= 10000000000000 AND open_time < 100000000000000000{where_sql}",
            tuple(params),
        )
        us_count = int(cur.fetchone()[0] or 0)
        if us_count:
            cur.execute(
                f"""
                UPDATE klines
                SET
                    open_time = open_time / 1000,
                    close_time = CASE WHEN close_time IS NULL THEN NULL ELSE close_time / 1000 END
                WHERE open_time >= 10000000000000 AND open_time < 100000000000000000{where_sql}
                """,
                tuple(params),
            )

        cur.execute(
            f"SELECT COUNT(*) FROM klines WHERE open_time >= 100000000000000000{where_sql}",
            tuple(params),
        )
        ns_count = int(cur.fetchone()[0] or 0)
        if ns_count:
            cur.execute(
                f"""
                UPDATE klines
                SET
                    open_time = open_time / 1000000,
                    close_time = CASE WHEN close_time IS NULL THEN NULL ELSE close_time / 1000000 END
                WHERE open_time >= 100000000000000000{where_sql}
                """,
                tuple(params),
            )
    conn.close()
    return {"fixed_seconds_rows": sec_count, "fixed_microseconds_rows": us_count, "fixed_nanoseconds_rows": ns_count}


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
            cured = cure_kline_row_format(r)
            to_insert.append(
                (
                    symbol,
                    interval,
                    int(cured[0]),
                    float(cured[1]),
                    float(cured[2]),
                    float(cured[3]),
                    float(cured[4]),
                    float(cured[5]),
                    int(cured[6]) if cured[6] is not None else None,
                    cured[7],
                    int(cured[8]),
                    cured[9],
                    cured[10],
                    str(cured[11]) if len(cured) > 11 else "",
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
    params: List[Any] = [symbol, interval]
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


def iter_query_klines(
    path: str,
    symbol: str,
    interval: str,
    start_ts: Optional[int] = None,
    end_ts: Optional[int] = None,
    fetch_size: int = 10000,
):
    """Yield kline rows in `fetch_size` batches without materializing all.

    Keeps RAM bounded for very large rage queries (e.g. 1s annual ~ 31M rows).
    The connection stays open until the generator is exhausted/closed.
    """
    conn = _connect(path)
    cur = conn.cursor()
    sql = "SELECT * FROM klines WHERE symbol=? AND interval=?"
    params: List[Any] = [symbol, interval]
    if start_ts is not None:
        sql += " AND open_time>=?"
        params.append(int(start_ts))
    if end_ts is not None:
        sql += " AND open_time<=?"
        params.append(int(end_ts))
    sql += " ORDER BY open_time ASC"
    try:
        cur.execute(sql, params)
        while True:
            rows = cur.fetchmany(max(1, int(fetch_size)))
            if not rows:
                return
            for row in rows:
                yield row
    finally:
        conn.close()


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
    run_id = 0

    def _op() -> None:
        nonlocal run_id
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
            run_id = int(cur.lastrowid or 0)

    _run_db_write_with_retry(_op)
    conn.close()
    return run_id


def finish_bt_run(path: str, run_id: int, status: str = "completed") -> None:
    conn = _connect(path)

    def _op() -> None:
        with conn:
            conn.execute(
                "UPDATE bt_runs SET status=?, ended_at=? WHERE run_id=?",
                (status, _utc_now(), int(run_id)),
            )

    _run_db_write_with_retry(_op)
    conn.close()


def insert_bt_events(
    path: str,
    run_id: int,
    events: Iterable[Dict[str, Any]],
    batch_size: int = 5000,
) -> None:
    """Insert run events using `executemany` flushed in batches.

    Batching keeps RAM bounded for very long runs (millions of events) and
    reduces SQLite lock-hold time, lowering contention when many workers
    write to neighbouring tables in the same DB file.
    """

    def _row(e: Dict[str, Any]) -> Tuple[Any, ...]:
        return (
            int(run_id),
            int(e["trial_id"]) if e.get("trial_id") is not None else None,
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

    batch_size = max(1, int(batch_size))
    buffer: List[Tuple[Any, ...]] = []
    conn = _connect(path)
    insert_sql = (
        "INSERT INTO bt_events ("
        "run_id, trial_id, seq, event_time, event_type, side, price, qty, cash,"
        " equity, position_qty, payload_json"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    def _flush(rows: List[Tuple[Any, ...]]) -> None:
        if not rows:
            return

        def _op() -> None:
            with conn:
                conn.executemany(insert_sql, rows)

        _run_db_write_with_retry(_op)

    try:
        for e in events:
            buffer.append(_row(e))
            if len(buffer) >= batch_size:
                _flush(buffer)
                buffer.clear()
        _flush(buffer)
    finally:
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

    def _op() -> None:
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

    _run_db_write_with_retry(_op)
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
        trial_id = int(cur.lastrowid or 0)
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


def get_bt_run_events(path: str, run_id: int) -> List[Tuple]:
    conn = _connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT seq, event_time, event_type, side, cash, equity, payload_json
        FROM bt_events
        WHERE run_id=?
        ORDER BY seq ASC
        """,
        (int(run_id),),
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


def get_bt_signal_events(path: str, run_id: int) -> List[Tuple]:
    conn = _connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT seq, event_time, event_type, side, price, qty, payload_json
        FROM bt_events
        WHERE run_id=? AND side IN ('buy', 'sell')
        ORDER BY seq ASC
        """,
        (int(run_id),),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_bt_run_events(path: str, run_id: int) -> List[Tuple]:
    conn = _connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT seq, event_time, event_type, side, cash, equity, payload_json
        FROM bt_events
        WHERE run_id=?
        ORDER BY seq ASC
        """,
        (int(run_id),),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def get_bt_run_descriptor(path: str, run_id: int) -> Optional[Dict[str, Any]]:
    conn = _connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            r.run_id,
            r.strategy_name,
            r.symbol,
            r.interval,
            r.start_ts,
            r.end_ts,
            r.initial_cash,
            r.fee_rate,
            r.slippage_bps,
            r.status,
            r.created_at,
            r.ended_at,
            MIN(e.event_time) AS first_event_time,
            MAX(e.event_time) AS last_event_time,
            COUNT(e.event_id) AS event_count
        FROM bt_runs r
        LEFT JOIN bt_events e ON e.run_id = r.run_id
        WHERE r.run_id = ?
        GROUP BY
            r.run_id, r.strategy_name, r.symbol, r.interval, r.start_ts, r.end_ts,
            r.initial_cash, r.fee_rate, r.slippage_bps, r.status, r.created_at, r.ended_at
        """,
        (int(run_id),),
    )
    row = cur.fetchone()
    conn.close()
    if not row:
        return None
    return {
        "run_id": int(row[0]),
        "strategy_name": row[1],
        "symbol": row[2],
        "interval": row[3],
        "config_start_ts": int(row[4]) if row[4] is not None else None,
        "config_end_ts": int(row[5]) if row[5] is not None else None,
        "initial_cash": float(row[6]),
        "fee_rate": float(row[7]),
        "slippage_bps": float(row[8]),
        "status": row[9],
        "created_at": row[10],
        "ended_at": row[11],
        "first_event_time": int(row[12]) if row[12] is not None else None,
        "last_event_time": int(row[13]) if row[13] is not None else None,
        "event_count": int(row[14]) if row[14] is not None else 0,
    }


def get_bt_study_trials(path: str, study_name: str, limit: int = 10_000) -> List[Tuple]:
    conn = _connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT trial_id, trial_number, state, objective, params_json, started_at, finished_at, duration_sec
        FROM bt_trials
        WHERE study_name=?
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
