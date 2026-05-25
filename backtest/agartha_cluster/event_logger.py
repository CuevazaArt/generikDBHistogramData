"""Structured event logger for the Agartha cluster.

Writes every event twice:

1. **Event log table** in cluster.db (structured, queryable).
2. **JSONL** append-only at ``logs/agartha_cluster/<YYYY-MM-DD>.jsonl``.

The JSONL stream is the canonical forensic record: rotation by UTC day,
flush on every write, append-only. It is intentionally easy to ship to
external alerting (Slack/Telegram/PagerDuty) by a sidecar process.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.models import (
    Event,
    EventKind,
    EventLevel,
    EventSource,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _utc_day(ms: Optional[int] = None) -> str:
    ts = (ms / 1000.0) if ms is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _utc_iso(ms: Optional[int] = None) -> str:
    ts = (ms / 1000.0) if ms is not None else time.time()
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class EventLogger:
    """Persist events to DB + JSONL with one call site.

    Durability
    ----------
    JSONL writes are **flushed + fsynced** by default (``fsync_jsonl=True``)
    so that a hard crash (power loss, kernel panic) does not lose the
    most recent forensic line. Set ``fsync_jsonl=False`` only in tests
    where the OS page cache flush cost matters (~20-30 us per event on
    a modern SSD).
    """

    def __init__(
        self,
        db: ClusterDB,
        *,
        jsonl_dir: str | os.PathLike[str] = "logs/agartha_cluster",
        echo_stdout: bool = True,
        fsync_jsonl: bool = True,
    ):
        self.db = db
        self.jsonl_dir = Path(jsonl_dir)
        self.jsonl_dir.mkdir(parents=True, exist_ok=True)
        self.echo_stdout = bool(echo_stdout)
        self.fsync_jsonl = bool(fsync_jsonl)

    def _jsonl_path(self, ts_ms: int) -> Path:
        return self.jsonl_dir / f"events_{_utc_day(ts_ms)}.jsonl"

    def log(
        self,
        *,
        kind: EventKind,
        source: EventSource,
        level: EventLevel = EventLevel.INFO,
        bot_id: Optional[int] = None,
        symbol: Optional[str] = None,
        correlation_id: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        ts_ms: Optional[int] = None,
    ) -> int:
        ts_ms = int(ts_ms if ts_ms is not None else _now_ms())
        event = Event(
            ts_ms=ts_ms,
            source=source,
            level=level,
            kind=kind,
            bot_id=bot_id,
            symbol=symbol,
            correlation_id=correlation_id,
            payload=dict(payload or {}),
        )

        event_id = self.db.log_event(event)

        record = {
            "event_id": event_id,
            "ts_utc": _utc_iso(ts_ms),
            "ts_ms": ts_ms,
            "source": event.source.value,
            "level": event.level.value,
            "kind": event.kind.value,
            "bot_id": event.bot_id,
            "symbol": event.symbol,
            "correlation_id": event.correlation_id,
            "payload": event.payload,
        }
        path = self._jsonl_path(ts_ms)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            if self.fsync_jsonl:
                fh.flush()
                try:
                    os.fsync(fh.fileno())
                except OSError:
                    # Some filesystems (e.g. mounted tmpfs in CI) reject fsync;
                    # the flush() above already pushed user-space buffers, so the
                    # data is at least in the OS page cache.
                    pass

        if self.echo_stdout:
            sym = f" {event.symbol}" if event.symbol else ""
            bot = f" bot={event.bot_id}" if event.bot_id else ""
            corr = f" corr={event.correlation_id}" if event.correlation_id else ""
            payload_str = ""
            if event.payload:
                short = {k: event.payload[k] for k in list(event.payload)[:4]}
                payload_str = f" {short}"
                if len(event.payload) > 4:
                    payload_str += " …"
            print(
                f"[{_utc_iso(ts_ms)}][{event.level.value:8s}][{event.source.value:11s}]"
                f"[{event.kind.value}]{sym}{bot}{corr}{payload_str}",
                flush=True,
            )

        return event_id

    # ------------------------------------------------------------------
    # Convenience shortcuts (kept short to encourage call-site clarity)
    # ------------------------------------------------------------------
    def info(self, *, kind: EventKind, source: EventSource, **kw) -> int:
        return self.log(kind=kind, source=source, level=EventLevel.INFO, **kw)

    def warn(self, *, kind: EventKind, source: EventSource, **kw) -> int:
        return self.log(kind=kind, source=source, level=EventLevel.WARNING, **kw)

    def error(self, *, kind: EventKind, source: EventSource, **kw) -> int:
        return self.log(kind=kind, source=source, level=EventLevel.ERROR, **kw)

    def critical(self, *, kind: EventKind, source: EventSource, **kw) -> int:
        return self.log(kind=kind, source=source, level=EventLevel.CRITICAL, **kw)
