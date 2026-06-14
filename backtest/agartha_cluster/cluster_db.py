"""SQLite DAO for the Agartha cluster.

Single-file, WAL mode. Concurrent readers (CLI inspectors, dashboards) do
not block the service's writes. All public methods are short, explicit
and use parameterised SQL. No ORM.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from backtest.agartha_cluster.models import (
    BotRecord,
    BotState,
    Event,
    EventKind,
    EventLevel,
    EventSource,
    OrderRecord,
    OrderSide,
    OrderState,
    OrderType,
    SymbolFilters,
    SymbolParams,
)

_MIGRATION_FILE = Path(__file__).parent / "migrations" / "V0001__cluster_schema.sql"


def _now_ms() -> int:
    return int(time.time() * 1000)


class ClusterDB:
    """Thin DAO over the cluster SQLite file.

    Notes
    -----
    - WAL is enabled on first connect.
    - `connect()` is per-call cheap; the class is safe to instantiate per
      operation in scripts. For the long-running service prefer one
      instance per task and call ``close()`` on shutdown.
    - All writes wrap in implicit transactions; long batches should use
      :meth:`transaction`.
    """

    def __init__(
        self,
        db_path: str | os.PathLike[str],
        *,
        synchronous: str = "FULL",
    ):
        """Open (lazily) a cluster DB.

        Parameters
        ----------
        db_path : path to the SQLite file.
        synchronous : SQLite synchronous mode (``FULL`` (default, recommended
            for production / power-loss safety), ``NORMAL`` (faster, default
            for tests), ``OFF`` (no fsync, do not use)). FULL forces an
            fsync of the WAL before commit AND on checkpoints; the only
            transactions that can be lost are those still in the OS page
            cache that were never persisted by the OS itself, which on
            modern Windows / macOS / Linux means a hard kernel panic. In
            practice this catches >99% of power-loss / unplug scenarios.
        """
        self.db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        if synchronous.upper() not in {"OFF", "NORMAL", "FULL", "EXTRA"}:
            raise ValueError(f"Invalid synchronous mode: {synchronous!r}")
        self._synchronous = synchronous.upper()

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(
                self.db_path,
                isolation_level=None,
                detect_types=sqlite3.PARSE_DECLTYPES,
                timeout=30.0,
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute(f"PRAGMA synchronous={self._synchronous};")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            # Auto-checkpoint after ~1000 pages (~4MB) keeps WAL bounded
            # even between explicit calls to wal_checkpoint().
            self._conn.execute("PRAGMA wal_autocheckpoint=1000;")
        return self._conn

    def wal_checkpoint(self, *, mode: str = "TRUNCATE") -> tuple[int, int, int]:
        """Force a WAL checkpoint. Recommended periodically in production.

        Modes (per SQLite docs):
          - ``PASSIVE``   : checkpoint without blocking writers.
          - ``FULL``      : block until all writers finish.
          - ``RESTART``   : like FULL + restart from page 0 next time.
          - ``TRUNCATE``  : like RESTART + shrink the WAL file to 0 bytes.

        Returns ``(busy, log_pages, checkpointed_pages)``.
        """
        mode = mode.upper()
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError(f"Invalid checkpoint mode: {mode!r}")
        conn = self.connect()
        row = conn.execute(f"PRAGMA wal_checkpoint({mode});").fetchone()
        if row is None:
            return (0, 0, 0)
        return (int(row[0] or 0), int(row[1] or 0), int(row[2] or 0))

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "ClusterDB":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------
    def init_schema(self) -> None:
        """Apply migration script idempotently."""
        sql_text = _MIGRATION_FILE.read_text(encoding="utf-8")
        conn = self.connect()
        conn.executescript(sql_text)

    def schema_version(self) -> Optional[str]:
        conn = self.connect()
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        return None if row is None else str(row["value"])

    # ------------------------------------------------------------------
    # Alpha universe
    # ------------------------------------------------------------------
    def upsert_universe(self, rows: Iterable[dict[str, Any]]) -> int:
        """Insert or update symbols in ``alpha_universe``. Returns count."""
        conn = self.connect()
        sql = """
        INSERT INTO alpha_universe(
            symbol, alpha_id, quote_asset, listing_ts, last_seen_ts,
            status, holders, liquidity_usd, metadata_json, updated_at
        ) VALUES (
            :symbol, :alpha_id, :quote_asset, :listing_ts, :last_seen_ts,
            COALESCE(:status, 'eligible'), :holders, :liquidity_usd,
            :metadata_json, datetime('now')
        )
        ON CONFLICT(symbol) DO UPDATE SET
            alpha_id       = excluded.alpha_id,
            quote_asset    = excluded.quote_asset,
            listing_ts     = excluded.listing_ts,
            last_seen_ts   = excluded.last_seen_ts,
            holders        = excluded.holders,
            liquidity_usd  = excluded.liquidity_usd,
            metadata_json  = excluded.metadata_json,
            updated_at     = datetime('now')
        """
        n = 0
        cur = conn.cursor()
        try:
            cur.execute("BEGIN")
            for row in rows:
                payload = {
                    "symbol": row["symbol"],
                    "alpha_id": row.get("alpha_id"),
                    "quote_asset": row.get("quote_asset"),
                    "listing_ts": row.get("listing_ts"),
                    "last_seen_ts": row.get("last_seen_ts"),
                    "status": row.get("status"),
                    "holders": row.get("holders"),
                    "liquidity_usd": row.get("liquidity_usd"),
                    "metadata_json": (
                        json.dumps(row["metadata"]) if "metadata" in row and row["metadata"] is not None
                        else row.get("metadata_json")
                    ),
                }
                cur.execute(sql, payload)
                n += 1
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return n

    def list_universe(self, status: Optional[str] = None) -> list[sqlite3.Row]:
        conn = self.connect()
        if status is None:
            return list(conn.execute("SELECT * FROM alpha_universe ORDER BY symbol"))
        return list(
            conn.execute(
                "SELECT * FROM alpha_universe WHERE status = ? ORDER BY symbol",
                (status,),
            )
        )

    def set_symbol_status(self, symbol: str, status: str) -> None:
        conn = self.connect()
        conn.execute(
            "UPDATE alpha_universe SET status = ?, updated_at = datetime('now') WHERE symbol = ?",
            (status, symbol),
        )

    # ------------------------------------------------------------------
    # Symbol params
    # ------------------------------------------------------------------
    def upsert_symbol_params(self, params: SymbolParams, raw_params: Optional[dict] = None) -> None:
        conn = self.connect()
        conn.execute(
            """
            INSERT INTO symbol_params(
                symbol, trailing_stop_pct, activation_profit_pct, breakeven_lock_pct,
                entry_limit_offset_pct, partial_tp_pct, partial_tp_size_pct,
                max_holding_bars, study_equity_pct, study_max_dd_pct,
                study_trial_id, optimized_at, optuna_db_path, raw_params_json
            ) VALUES (
                :symbol, :trailing_stop_pct, :activation_profit_pct, :breakeven_lock_pct,
                :entry_limit_offset_pct, :partial_tp_pct, :partial_tp_size_pct,
                :max_holding_bars, :study_equity_pct, :study_max_dd_pct,
                :study_trial_id, :optimized_at, :optuna_db_path, :raw_params_json
            )
            ON CONFLICT(symbol) DO UPDATE SET
                trailing_stop_pct      = excluded.trailing_stop_pct,
                activation_profit_pct  = excluded.activation_profit_pct,
                breakeven_lock_pct     = excluded.breakeven_lock_pct,
                entry_limit_offset_pct = excluded.entry_limit_offset_pct,
                partial_tp_pct         = excluded.partial_tp_pct,
                partial_tp_size_pct    = excluded.partial_tp_size_pct,
                max_holding_bars       = excluded.max_holding_bars,
                study_equity_pct       = excluded.study_equity_pct,
                study_max_dd_pct       = excluded.study_max_dd_pct,
                study_trial_id         = excluded.study_trial_id,
                optimized_at           = excluded.optimized_at,
                optuna_db_path         = excluded.optuna_db_path,
                raw_params_json        = excluded.raw_params_json
            """,
            {
                "symbol": params.symbol,
                "trailing_stop_pct": params.trailing_stop_pct,
                "activation_profit_pct": params.activation_profit_pct,
                "breakeven_lock_pct": params.breakeven_lock_pct,
                "entry_limit_offset_pct": params.entry_limit_offset_pct,
                "partial_tp_pct": params.partial_tp_pct,
                "partial_tp_size_pct": params.partial_tp_size_pct,
                "max_holding_bars": params.max_holding_bars,
                "study_equity_pct": params.study_equity_pct,
                "study_max_dd_pct": params.study_max_dd_pct,
                "study_trial_id": params.study_trial_id,
                "optimized_at": params.optimized_at,
                "optuna_db_path": params.optuna_db_path,
                "raw_params_json": json.dumps(raw_params) if raw_params else None,
            },
        )

    def get_symbol_params(self, symbol: str) -> Optional[SymbolParams]:
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM symbol_params WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            return None
        return SymbolParams(
            symbol=row["symbol"],
            trailing_stop_pct=float(row["trailing_stop_pct"]),
            activation_profit_pct=float(row["activation_profit_pct"]),
            breakeven_lock_pct=float(row["breakeven_lock_pct"]),
            entry_limit_offset_pct=float(row["entry_limit_offset_pct"] or 0),
            partial_tp_pct=float(row["partial_tp_pct"] or 0),
            partial_tp_size_pct=float(row["partial_tp_size_pct"] or 0),
            max_holding_bars=int(row["max_holding_bars"] or 0),
            study_equity_pct=row["study_equity_pct"],
            study_max_dd_pct=row["study_max_dd_pct"],
            study_trial_id=row["study_trial_id"],
            optimized_at=row["optimized_at"],
            optuna_db_path=row["optuna_db_path"],
        )

    # ------------------------------------------------------------------
    # Symbol filters
    # ------------------------------------------------------------------
    def upsert_symbol_filters(self, filters: SymbolFilters, raw: Optional[dict] = None) -> None:
        conn = self.connect()
        conn.execute(
            """
            INSERT INTO symbol_filters(
                symbol, tick_size, step_size, min_notional,
                bid_multiplier_up, bid_multiplier_down,
                ask_multiplier_up, ask_multiplier_down,
                refreshed_at, raw_filters_json
            ) VALUES (
                :symbol, :tick_size, :step_size, :min_notional,
                :bid_mu, :bid_md, :ask_mu, :ask_md,
                :refreshed_at, :raw
            )
            ON CONFLICT(symbol) DO UPDATE SET
                tick_size           = excluded.tick_size,
                step_size           = excluded.step_size,
                min_notional        = excluded.min_notional,
                bid_multiplier_up   = excluded.bid_multiplier_up,
                bid_multiplier_down = excluded.bid_multiplier_down,
                ask_multiplier_up   = excluded.ask_multiplier_up,
                ask_multiplier_down = excluded.ask_multiplier_down,
                refreshed_at        = excluded.refreshed_at,
                raw_filters_json    = excluded.raw_filters_json
            """,
            {
                "symbol": filters.symbol,
                "tick_size": filters.tick_size,
                "step_size": filters.step_size,
                "min_notional": filters.min_notional,
                "bid_mu": filters.bid_multiplier_up,
                "bid_md": filters.bid_multiplier_down,
                "ask_mu": filters.ask_multiplier_up,
                "ask_md": filters.ask_multiplier_down,
                "refreshed_at": filters.refreshed_at,
                "raw": json.dumps(raw) if raw else None,
            },
        )

    def get_symbol_filters(self, symbol: str) -> Optional[SymbolFilters]:
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM symbol_filters WHERE symbol = ?", (symbol,)
        ).fetchone()
        if row is None:
            return None
        return SymbolFilters(
            symbol=row["symbol"],
            tick_size=float(row["tick_size"]),
            step_size=float(row["step_size"]),
            min_notional=float(row["min_notional"]),
            bid_multiplier_up=float(row["bid_multiplier_up"]),
            bid_multiplier_down=float(row["bid_multiplier_down"]),
            ask_multiplier_up=float(row["ask_multiplier_up"]),
            ask_multiplier_down=float(row["ask_multiplier_down"]),
            refreshed_at=row["refreshed_at"],
        )

    # ------------------------------------------------------------------
    # Bots
    # ------------------------------------------------------------------
    def create_bot(
        self,
        *,
        symbol: str,
        capital_usdt: float,
        params_snapshot: dict,
        correlation_id: str,
        state: BotState = BotState.CREATED,
    ) -> int:
        conn = self.connect()
        cur = conn.execute(
            """
            INSERT INTO cluster_bots(symbol, state, capital_usdt, params_snapshot_json, correlation_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                symbol,
                state.value,
                float(capital_usdt),
                json.dumps(params_snapshot, sort_keys=True),
                correlation_id,
            ),
        )
        bot_id = int(cur.lastrowid)
        self.append_state_log(
            bot_id=bot_id,
            from_state=None,
            to_state=state,
            reason="created",
            correlation_id=correlation_id,
        )
        return bot_id

    def update_bot(self, bot_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = :{k}" for k in fields)
        params = dict(fields)
        params["bot_id"] = bot_id
        params["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        sql = f"UPDATE cluster_bots SET {cols}, updated_at = :updated_at WHERE bot_id = :bot_id"
        conn = self.connect()
        conn.execute(sql, params)

    def get_bot(self, bot_id: int) -> Optional[BotRecord]:
        conn = self.connect()
        row = conn.execute(
            "SELECT * FROM cluster_bots WHERE bot_id = ?", (bot_id,)
        ).fetchone()
        if row is None:
            return None
        return BotRecord(
            bot_id=int(row["bot_id"]),
            symbol=row["symbol"],
            state=BotState(row["state"]),
            capital_usdt=float(row["capital_usdt"]),
            params_snapshot_json=row["params_snapshot_json"],
            correlation_id=row["correlation_id"],
            entry_order_id=row["entry_order_id"],
            entry_client_order_id=row["entry_client_order_id"],
            entry_price=row["entry_price"],
            entry_qty=row["entry_qty"],
            entry_filled_ts=row["entry_filled_ts"],
            peak_price=row["peak_price"],
            trail_floor=row["trail_floor"],
            exit_order_id=row["exit_order_id"],
            exit_client_order_id=row["exit_client_order_id"],
            exit_price=row["exit_price"],
            exit_qty=row["exit_qty"],
            exit_filled_ts=row["exit_filled_ts"],
            realized_pnl_usdt=row["realized_pnl_usdt"],
            deployed_at=row["deployed_at"],
            closed_at=row["closed_at"],
            notes=row["notes"],
        )

    def list_bots(
        self,
        *,
        state: Optional[BotState] = None,
        symbol: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[BotRecord]:
        conn = self.connect()
        clauses: list[str] = []
        params: list[Any] = []
        if state is not None:
            clauses.append("state = ?")
            params.append(state.value)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        sql = "SELECT * FROM cluster_bots"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY bot_id DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = conn.execute(sql, params).fetchall()
        return [
            BotRecord(
                bot_id=int(r["bot_id"]),
                symbol=r["symbol"],
                state=BotState(r["state"]),
                capital_usdt=float(r["capital_usdt"]),
                params_snapshot_json=r["params_snapshot_json"],
                correlation_id=r["correlation_id"],
                entry_order_id=r["entry_order_id"],
                entry_client_order_id=r["entry_client_order_id"],
                entry_price=r["entry_price"],
                entry_qty=r["entry_qty"],
                entry_filled_ts=r["entry_filled_ts"],
                peak_price=r["peak_price"],
                trail_floor=r["trail_floor"],
                exit_order_id=r["exit_order_id"],
                exit_client_order_id=r["exit_client_order_id"],
                exit_price=r["exit_price"],
                exit_qty=r["exit_qty"],
                exit_filled_ts=r["exit_filled_ts"],
                realized_pnl_usdt=r["realized_pnl_usdt"],
                deployed_at=r["deployed_at"],
                closed_at=r["closed_at"],
                notes=r["notes"],
            )
            for r in rows
        ]

    def append_state_log(
        self,
        *,
        bot_id: int,
        from_state: Optional[BotState],
        to_state: BotState,
        reason: Optional[str],
        correlation_id: Optional[str] = None,
        ts_ms: Optional[int] = None,
    ) -> None:
        conn = self.connect()
        conn.execute(
            """
            INSERT INTO bot_state_log(bot_id, from_state, to_state, reason, ts_ms, correlation_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                bot_id,
                from_state.value if from_state else None,
                to_state.value,
                reason,
                int(ts_ms if ts_ms is not None else _now_ms()),
                correlation_id,
            ),
        )

    # ------------------------------------------------------------------
    # Deploy queue
    # ------------------------------------------------------------------
    def enqueue_deploy(
        self,
        *,
        symbol: str,
        planned_deploy_ts: int,
        priority: int = 100,
        reason: Optional[str] = None,
    ) -> Optional[int]:
        """Insert a planned deploy. Returns queue_id or None if duplicate active."""
        conn = self.connect()
        try:
            cur = conn.execute(
                """
                INSERT INTO deploy_queue(symbol, planned_deploy_ts, priority, status, reason)
                VALUES (?, ?, ?, 'planned', ?)
                """,
                (symbol, int(planned_deploy_ts), int(priority), reason),
            )
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def next_due_deploy(self, *, now_ms: Optional[int] = None) -> Optional[sqlite3.Row]:
        """Return the next planned deploy whose planned_deploy_ts has passed."""
        now_ms = int(now_ms if now_ms is not None else _now_ms())
        conn = self.connect()
        return conn.execute(
            """
            SELECT * FROM deploy_queue
            WHERE status IN ('planned','ready')
              AND planned_deploy_ts <= ?
            ORDER BY priority ASC, planned_deploy_ts ASC, queue_id ASC
            LIMIT 1
            """,
            (now_ms,),
        ).fetchone()

    def mark_queue_status(
        self,
        queue_id: int,
        status: str,
        *,
        bot_id: Optional[int] = None,
        actual_deploy_ts: Optional[int] = None,
        reason: Optional[str] = None,
    ) -> None:
        conn = self.connect()
        conn.execute(
            """
            UPDATE deploy_queue
            SET status = ?,
                bot_id = COALESCE(?, bot_id),
                actual_deploy_ts = COALESCE(?, actual_deploy_ts),
                reason = COALESCE(?, reason)
            WHERE queue_id = ?
            """,
            (status, bot_id, actual_deploy_ts, reason, queue_id),
        )

    def last_deploy_ts(self) -> Optional[int]:
        conn = self.connect()
        row = conn.execute(
            "SELECT MAX(actual_deploy_ts) AS m FROM deploy_queue WHERE actual_deploy_ts IS NOT NULL"
        ).fetchone()
        return None if row is None or row["m"] is None else int(row["m"])

    # ------------------------------------------------------------------
    # Orders + fills
    # ------------------------------------------------------------------
    def insert_order(self, order: OrderRecord) -> int:
        conn = self.connect()
        cur = conn.execute(
            """
            INSERT INTO orders(
                order_id, client_order_id, bot_id, symbol, side, order_type, state,
                price, qty, filled_qty, avg_fill_price, submitted_ts, last_update_ts,
                correlation_id, raw_response
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order.order_id,
                order.client_order_id,
                order.bot_id,
                order.symbol,
                order.side.value,
                order.order_type.value,
                order.state.value,
                float(order.price),
                float(order.qty),
                float(order.filled_qty),
                float(order.avg_fill_price),
                order.submitted_ts,
                order.last_update_ts,
                order.correlation_id,
                order.raw_response,
            ),
        )
        return int(cur.lastrowid)

    def update_order_state(
        self,
        *,
        client_order_id: str,
        state: OrderState,
        filled_qty: Optional[float] = None,
        avg_fill_price: Optional[float] = None,
        order_id: Optional[str] = None,
        raw_response: Optional[str] = None,
    ) -> None:
        conn = self.connect()
        conn.execute(
            """
            UPDATE orders SET
                state = ?,
                filled_qty = COALESCE(?, filled_qty),
                avg_fill_price = COALESCE(?, avg_fill_price),
                order_id = COALESCE(?, order_id),
                raw_response = COALESCE(?, raw_response),
                last_update_ts = ?
            WHERE client_order_id = ?
            """,
            (
                state.value,
                filled_qty,
                avg_fill_price,
                order_id,
                raw_response,
                _now_ms(),
                client_order_id,
            ),
        )

    def get_order_by_client_id(self, client_order_id: str) -> Optional[sqlite3.Row]:
        conn = self.connect()
        return conn.execute(
            "SELECT * FROM orders WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()

    def insert_fill(
        self,
        *,
        bot_id: int,
        order_pk: Optional[int],
        exchange_fill_id: Optional[str],
        symbol: str,
        side: OrderSide,
        price: float,
        qty: float,
        fee: float,
        fee_asset: Optional[str],
        ts_ms: int,
        is_maker: bool,
        correlation_id: Optional[str],
        raw_payload: Optional[str],
    ) -> int:
        conn = self.connect()
        cur = conn.execute(
            """
            INSERT INTO fills(
                bot_id, order_pk, exchange_fill_id, symbol, side, price, qty, fee,
                fee_asset, ts_ms, is_maker, correlation_id, raw_payload
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bot_id,
                order_pk,
                exchange_fill_id,
                symbol,
                side.value,
                float(price),
                float(qty),
                float(fee),
                fee_asset,
                int(ts_ms),
                1 if is_maker else 0,
                correlation_id,
                raw_payload,
            ),
        )
        return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # API calls
    # ------------------------------------------------------------------
    def log_api_call(
        self,
        *,
        endpoint: str,
        method: str,
        weight: int,
        status_code: Optional[int],
        latency_ms: Optional[int],
        bot_id: Optional[int],
        correlation_id: Optional[str],
        request_summary: Optional[str] = None,
        error_text: Optional[str] = None,
    ) -> int:
        conn = self.connect()
        cur = conn.execute(
            """
            INSERT INTO api_calls(
                ts_ms, endpoint, method, weight, status_code, latency_ms,
                bot_id, correlation_id, request_summary, error_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_ms(),
                endpoint,
                method,
                int(weight),
                status_code,
                latency_ms,
                bot_id,
                correlation_id,
                request_summary,
                error_text,
            ),
        )
        return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # Event log
    # ------------------------------------------------------------------
    def log_event(self, event: Event) -> int:
        conn = self.connect()
        cur = conn.execute(
            """
            INSERT INTO event_log(
                ts_ms, source, level, kind, bot_id, symbol, correlation_id, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(event.ts_ms),
                event.source.value,
                event.level.value,
                event.kind.value,
                event.bot_id,
                event.symbol,
                event.correlation_id,
                json.dumps(event.payload, ensure_ascii=False, default=str) if event.payload else None,
            ),
        )
        return int(cur.lastrowid)

    def query_events(
        self,
        *,
        bot_id: Optional[int] = None,
        symbol: Optional[str] = None,
        kind: Optional[EventKind] = None,
        level: Optional[EventLevel] = None,
        since_ms: Optional[int] = None,
        limit: int = 200,
    ) -> list[sqlite3.Row]:
        conn = self.connect()
        clauses: list[str] = []
        params: list[Any] = []
        if bot_id is not None:
            clauses.append("bot_id = ?")
            params.append(bot_id)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(symbol)
        if kind is not None:
            clauses.append("kind = ?")
            params.append(kind.value)
        if level is not None:
            clauses.append("level = ?")
            params.append(level.value)
        if since_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(int(since_ms))
        sql = "SELECT * FROM event_log"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts_ms DESC LIMIT ?"
        params.append(int(limit))
        return list(conn.execute(sql, params))

    # ------------------------------------------------------------------
    # API throttle buckets
    # ------------------------------------------------------------------
    def bump_throttle(self, *, minute_bucket: int, weight: int, orders: int) -> tuple[int, int]:
        """Increment counters atomically; return (weight_used, orders_count)."""
        conn = self.connect()
        with self.transaction():
            conn.execute(
                """
                INSERT INTO api_throttle_buckets(minute_bucket, weight_used, orders_count, updated_at)
                VALUES (?, ?, ?, datetime('now'))
                ON CONFLICT(minute_bucket) DO UPDATE SET
                    weight_used  = weight_used  + excluded.weight_used,
                    orders_count = orders_count + excluded.orders_count,
                    updated_at   = excluded.updated_at
                """,
                (int(minute_bucket), int(weight), int(orders)),
            )
            row = conn.execute(
                "SELECT weight_used, orders_count FROM api_throttle_buckets WHERE minute_bucket = ?",
                (int(minute_bucket),),
            ).fetchone()
        return (int(row["weight_used"]), int(row["orders_count"]))

    def get_throttle_bucket(self, minute_bucket: int) -> tuple[int, int]:
        conn = self.connect()
        row = conn.execute(
            "SELECT weight_used, orders_count FROM api_throttle_buckets WHERE minute_bucket = ?",
            (int(minute_bucket),),
        ).fetchone()
        if row is None:
            return (0, 0)
        return (int(row["weight_used"]), int(row["orders_count"]))

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    def insert_reconciliation(
        self,
        *,
        ts_ms: int,
        open_orders_count: int,
        positions_count: int,
        total_equity_usdt: Optional[float],
        drift_detected: bool,
        snapshot: dict,
    ) -> int:
        conn = self.connect()
        cur = conn.execute(
            """
            INSERT INTO reconciliation_snapshots(
                ts_ms, open_orders_count, positions_count, total_equity_usdt,
                drift_detected, snapshot_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                int(ts_ms),
                int(open_orders_count),
                int(positions_count),
                total_equity_usdt,
                1 if drift_detected else 0,
                json.dumps(snapshot, ensure_ascii=False, default=str),
            ),
        )
        return int(cur.lastrowid)

    # ------------------------------------------------------------------
    # Service runs
    # ------------------------------------------------------------------
    def start_service_run(self, *, mode: str, pid: int, host: str, version: str) -> int:
        conn = self.connect()
        cur = conn.execute(
            """
            INSERT INTO service_runs(started_at, mode, pid, host, version)
            VALUES (datetime('now'), ?, ?, ?, ?)
            """,
            (mode, pid, host, version),
        )
        return int(cur.lastrowid)

    def stop_service_run(self, run_id: int, *, reason: Optional[str]) -> None:
        conn = self.connect()
        conn.execute(
            "UPDATE service_runs SET stopped_at = datetime('now'), stop_reason = ? WHERE run_id = ?",
            (reason, run_id),
        )

    def list_open_service_runs(
        self,
        *,
        exclude_run_id: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        """Return service_runs rows whose ``stopped_at IS NULL``.

        Used by :meth:`ClusterService.recovery_boot` to detect previous
        runs that died without a graceful shutdown.
        """
        conn = self.connect()
        if exclude_run_id is None:
            return list(
                conn.execute(
                    "SELECT * FROM service_runs WHERE stopped_at IS NULL "
                    "ORDER BY run_id"
                )
            )
        return list(
            conn.execute(
                "SELECT * FROM service_runs WHERE stopped_at IS NULL "
                "AND run_id <> ? ORDER BY run_id",
                (int(exclude_run_id),),
            )
        )

    # ------------------------------------------------------------------
    # Queries used by recovery
    # ------------------------------------------------------------------
    def list_orders_by_state(self, states: list[str]) -> list[sqlite3.Row]:
        """Return orders whose ``state`` is in the given list."""
        if not states:
            return []
        placeholders = ",".join("?" for _ in states)
        conn = self.connect()
        return list(
            conn.execute(
                f"SELECT * FROM orders WHERE state IN ({placeholders}) "
                "ORDER BY order_pk",
                tuple(states),
            )
        )

    def count_fills_for_order(self, order_pk: int) -> int:
        conn = self.connect()
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM fills WHERE order_pk = ?",
            (int(order_pk),),
        ).fetchone()
        return int(row["n"] if row else 0)

    def sum_fills_for_order(self, order_pk: int) -> tuple[float, float, float]:
        """Aggregate all fills for an order.

        Returns ``(total_qty, vwap_price, total_fee)``.  VWAP is
        volume-weighted average price = sum(price*qty) / sum(qty).
        Returns ``(0.0, 0.0, 0.0)`` if no fills exist.
        """
        conn = self.connect()
        row = conn.execute(
            """
            SELECT COALESCE(SUM(qty), 0.0)         AS total_qty,
                   COALESCE(SUM(price * qty), 0.0)  AS notional,
                   COALESCE(SUM(fee), 0.0)           AS total_fee
            FROM fills WHERE order_pk = ?
            """,
            (int(order_pk),),
        ).fetchone()
        total_qty = float(row["total_qty"])
        notional = float(row["notional"])
        total_fee = float(row["total_fee"])
        vwap = notional / total_qty if total_qty > 0 else 0.0
        return (total_qty, vwap, total_fee)

    def purge_throttle_buckets_older_than(self, *, before_minute_bucket: int) -> int:
        """Delete throttle buckets whose ``minute_bucket`` is older than the
        given threshold (UNIX-minute units). Returns the row count deleted.
        """
        conn = self.connect()
        cur = conn.execute(
            "DELETE FROM api_throttle_buckets WHERE minute_bucket < ?",
            (int(before_minute_bucket),),
        )
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------------
    # Credentials meta
    # ------------------------------------------------------------------
    def upsert_credentials_meta(
        self,
        *,
        profile: str,
        service_name: str,
        username: str,
        storage_method: str,
    ) -> None:
        conn = self.connect()
        conn.execute(
            """
            INSERT INTO credentials_meta(profile, service_name, username, storage_method, created_at)
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(profile) DO UPDATE SET
                service_name = excluded.service_name,
                username = excluded.username,
                storage_method = excluded.storage_method,
                last_used_at = datetime('now')
            """,
            (profile, service_name, username, storage_method),
        )

    def get_credentials_meta(self, profile: str = "default") -> Optional[sqlite3.Row]:
        conn = self.connect()
        return conn.execute(
            "SELECT * FROM credentials_meta WHERE profile = ?", (profile,)
        ).fetchone()

    # ------------------------------------------------------------------
    # Resource metrics
    # ------------------------------------------------------------------
    def insert_resource_metric(
        self,
        *,
        ts_ms: int,
        proc_cpu_pct: float,
        proc_ram_mb: float,
        host_cpu_pct: float,
        host_ram_pct: float,
        disk_used_gb: float,
        disk_free_gb: float,
        disk_pct: float,
    ) -> int:
        conn = self.connect()
        cur = conn.execute(
            """
            INSERT INTO resource_metrics(
                ts_ms, proc_cpu_pct, proc_ram_mb, host_cpu_pct, host_ram_pct,
                disk_used_gb, disk_free_gb, disk_pct
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(ts_ms),
                float(proc_cpu_pct),
                float(proc_ram_mb),
                float(host_cpu_pct),
                float(host_ram_pct),
                float(disk_used_gb),
                float(disk_free_gb),
                float(disk_pct),
            ),
        )
        return int(cur.lastrowid)

    def get_resource_metrics(
        self,
        *,
        since_ms: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[sqlite3.Row]:
        conn = self.connect()
        clauses: list[str] = []
        params: list[Any] = []
        if since_ms is not None:
            clauses.append("ts_ms >= ?")
            params.append(int(since_ms))
        sql = "SELECT * FROM resource_metrics"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts_ms ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return list(conn.execute(sql, params))

