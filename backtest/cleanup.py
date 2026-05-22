"""Database housekeeping helpers.

Used to:
- Abort stale `bt_runs` that were left in `running` state after a crash, a
  manual kill (Ctrl+C) or a SQLite contention storm.
- Optionally trim `bt_events` for aborted runs, where data is incomplete.

Run automatically at the start of `optimize` and exposed via the CLI so the
user can clean up between sessions.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Dict


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def abort_stale_runs(db_path: str) -> Dict[str, int]:
    """Mark any `running` run as `aborted` and timestamp it.

    Returns a small report dict so callers can log the result.
    """
    conn = sqlite3.connect(db_path, timeout=60.0)
    try:
        cur = conn.cursor()
        before = cur.execute(
            "SELECT COUNT(1) FROM bt_runs WHERE status='running'"
        ).fetchone()[0]
        cur.execute(
            "UPDATE bt_runs SET status='aborted', ended_at=? WHERE status='running'",
            (_now_utc_iso(),),
        )
        conn.commit()
    finally:
        conn.close()
    return {"aborted_runs": int(before)}


def purge_aborted_run_events(db_path: str) -> Dict[str, int]:
    """Delete bt_events belonging to aborted runs.

    Aborted runs have partial/inconsistent events that only waste disk space
    and slow down queries; they cannot be analysed meaningfully anyway.
    """
    conn = sqlite3.connect(db_path, timeout=60.0)
    try:
        cur = conn.cursor()
        deleted = cur.execute(
            "DELETE FROM bt_events WHERE run_id IN (SELECT run_id FROM bt_runs WHERE status='aborted')"
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    return {"deleted_events": int(deleted or 0)}
