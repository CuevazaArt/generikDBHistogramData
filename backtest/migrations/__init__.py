"""Raw SQL migration runner for PostgreSQL.

Intentionally tiny: each `V<version>__<name>.sql` file under `sql/` is applied in
lexicographic order, inside its own transaction, and recorded in
`meta.schema_migrations`. SQLAlchemy is avoided here to keep the bootstrap path
free of ORM machinery.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional

import psycopg


_VERSION_RE = re.compile(r"^V(?P<version>\d+)__[A-Za-z0-9_\-]+\.sql$")
_SQL_DIR = Path(__file__).resolve().parent / "sql"

_BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS meta;
CREATE TABLE IF NOT EXISTS meta.schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ DEFAULT NOW()
);
"""


def _strip_sqlalchemy_prefix(dsn: str) -> str:
    """psycopg 3 accepts `postgresql://` but not the SQLAlchemy `+psycopg` form."""
    return dsn.replace("postgresql+psycopg://", "postgresql://", 1)


def list_migration_files() -> List[Path]:
    if not _SQL_DIR.is_dir():
        return []
    files: List[Path] = []
    for entry in sorted(_SQL_DIR.iterdir()):
        if entry.is_file() and _VERSION_RE.match(entry.name):
            files.append(entry)
    return files


def _version_of(path: Path) -> str:
    match = _VERSION_RE.match(path.name)
    if not match:
        raise ValueError(f"Bad migration filename: {path.name}")
    return match.group("version")


def _applied_versions(conn: "psycopg.Connection") -> set:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version FROM meta.schema_migrations ORDER BY version ASC"
        )
        return {row[0] for row in cur.fetchall()}


def apply_migrations(dsn: str) -> List[str]:
    """Apply pending migrations and return the list of versions that ran.

    Each pending file is executed in its own transaction. If any statement
    fails the transaction is rolled back, the exception propagates, and the
    bookkeeping row is NOT inserted. Already-applied files are skipped.
    """
    raw_dsn = _strip_sqlalchemy_prefix(dsn)
    applied: List[str] = []
    with psycopg.connect(raw_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(_BOOTSTRAP_SQL)
        conn.commit()

        existing = _applied_versions(conn)
        for path in list_migration_files():
            version = _version_of(path)
            if version in existing:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.transaction():
                with conn.cursor() as cur:
                    cur.execute(sql)
                    cur.execute(
                        "INSERT INTO meta.schema_migrations (version) VALUES (%s)",
                        (version,),
                    )
            applied.append(version)
    return applied


def current_version(dsn: str) -> Optional[str]:
    """Return the highest applied migration version, or None if the table is empty/missing."""
    raw_dsn = _strip_sqlalchemy_prefix(dsn)
    try:
        with psycopg.connect(raw_dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT version FROM meta.schema_migrations
                    ORDER BY version DESC LIMIT 1
                    """
                )
                row = cur.fetchone()
                return row[0] if row else None
    except psycopg.errors.UndefinedTable:
        return None
    except psycopg.OperationalError:
        raise


__all__ = [
    "apply_migrations",
    "current_version",
    "list_migration_files",
]
