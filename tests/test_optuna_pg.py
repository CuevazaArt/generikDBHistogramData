"""Tests for backtest.optuna_storage."""
from __future__ import annotations

import os
import sys
import urllib.parse
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.config import AppConfig
from backtest.optuna_storage import build_storage, ensure_optuna_schema


def _build_config(**overrides) -> AppConfig:
    base: dict = {
        "pg_dsn": None,
        "metadata_backend": "sqlite",
        "data_root": "data",
        "sqlite_path": "klines.db",
        "engine_kind": "python",
        "events_mode": "lite",
    }
    base.update(overrides)
    return AppConfig(**base)


def test_build_storage_sqlite_fallback(tmp_path):
    cfg = _build_config(sqlite_path=str(tmp_path / "klines.db"))
    url = build_storage("smoke_study", cfg)
    assert url.startswith("sqlite:///")
    assert url.endswith("klines.db")


def test_build_storage_sqlite_when_pg_missing_dsn():
    # backend == 'pg' but no DSN should still fall through to sqlite path.
    cfg = _build_config(metadata_backend="pg", pg_dsn=None)
    url = build_storage("study", cfg)
    assert url.startswith("sqlite:///")


def test_build_storage_pg_url_well_formed():
    dsn = "postgresql://user:pw@localhost:5433/db"
    cfg = _build_config(metadata_backend="pg", pg_dsn=dsn)
    url = build_storage("any_study", cfg)
    assert url.startswith("postgresql+psycopg://"), url
    # search_path=optuna,public must round-trip through urlencode.
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    assert "options" in query
    options_value = query["options"][0]
    assert "search_path=optuna,public" in options_value or "search_path=optuna%2Cpublic" in options_value
    # Raw URL must contain the URL-encoded form when round-tripped raw.
    assert "search_path%3Doptuna" in url or "search_path=optuna" in url


def test_build_storage_pg_url_preserves_existing_query():
    dsn = "postgresql+psycopg://user:pw@host:5432/db?sslmode=require"
    cfg = _build_config(metadata_backend="pg", pg_dsn=dsn)
    url = build_storage("study_q", cfg)
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    assert "sslmode" in query
    assert query["sslmode"][0] == "require"
    assert "options" in query


@pytest.mark.skipif(os.getenv("PG_DSN") is None, reason="PG not available")
def test_ensure_optuna_schema_skipif_no_pg():
    dsn = os.environ["PG_DSN"]
    # Should be idempotent.
    ensure_optuna_schema(dsn)
    ensure_optuna_schema(dsn)
