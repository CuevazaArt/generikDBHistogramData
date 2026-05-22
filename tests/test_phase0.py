"""Phase 0 tests: storage paths, idempotency, migrations, config and an
optional smoke test against a live PostgreSQL when `PG_DSN` is set.

All tests must pass without a running PostgreSQL. The PG-touching test is
marked `skipif` so CI without PG cleanly skips it.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.config import AppConfig
from backtest.idempotency import canonical_params_json, compute_run_key
from backtest import migrations as migrations_pkg
from backtest.storage_paths import StoragePaths, tmp_then_rename


def test_storage_paths_layout(tmp_path):
    paths = StoragePaths(data_root=str(tmp_path))

    klines = paths.klines_partition("XRPUSDT", "1h", 2024, 3)
    expected = str(
        tmp_path
        / "klines"
        / "symbol=XRPUSDT"
        / "interval=1h"
        / "year=2024"
        / "month=03"
        / "part-000.parquet"
    )
    assert klines == expected

    assert paths.klines_manifest() == str(tmp_path / "klines" / "_manifest.json")

    assert paths.events_dir(42) == str(tmp_path / "events" / "run_42")
    assert paths.events_part(42, 0) == str(tmp_path / "events" / "run_42" / "part-000.parquet")
    assert paths.events_part(42, 17) == str(tmp_path / "events" / "run_42" / "part-017.parquet")

    assert paths.equity_dir(42) == str(tmp_path / "equity" / "run_42")
    assert paths.equity_file(42) == str(tmp_path / "equity" / "run_42" / "equity.parquet")

    assert paths.checkpoints_dir(42) == str(tmp_path / "checkpoints" / "run_42")
    assert paths.checkpoint_file(42, 1_700_000_000) == str(
        tmp_path / "checkpoints" / "run_42" / "cp_1700000000.parquet"
    )

    assert paths.derived_dir("sweet_spot") == str(tmp_path / "derived" / "sweet_spot")

    created = paths.ensure_run_layout(7)
    assert os.path.isdir(created["events_dir"])
    assert os.path.isdir(created["equity_dir"])
    assert os.path.isdir(created["checkpoints_dir"])


def test_tmp_then_rename_atomic(tmp_path):
    target = tmp_path / "atomic.txt"
    with tmp_then_rename(str(target)) as tmp:
        Path(tmp).write_text("hello", encoding="utf-8")
        assert not target.exists()
    assert target.read_text(encoding="utf-8") == "hello"

    with pytest.raises(RuntimeError):
        with tmp_then_rename(str(target)) as tmp:
            Path(tmp).write_text("partial", encoding="utf-8")
            raise RuntimeError("simulated writer crash")
    assert target.read_text(encoding="utf-8") == "hello"


def test_idempotency_canonical_dict_order():
    a = {"a": 1.0, "b": 2.0, "c": [3, 1, 2]}
    b = {"c": [3, 1, 2], "b": 2.0, "a": 1.0}
    assert canonical_params_json(a) == canonical_params_json(b)


def test_idempotency_canonical_float_precision():
    base = {"x": 0.123456789012, "y": 1.0}
    noisy = {"x": 0.1234567890124999, "y": 1.0}
    assert canonical_params_json(base) == canonical_params_json(noisy)


def test_idempotency_compute_run_key_stable():
    payload = dict(
        strategy="dorothy_pf",
        symbol="XRPUSDT",
        interval="1m",
        start_ts=1_700_000_000,
        end_ts=1_730_000_000,
        initial_cash=10_000.0,
        fee_rate=0.001,
        slippage_bps=2.0,
        engine_kind="python",
        engine_version="0.1.0",
    )
    key_a = compute_run_key(strategy_params={"x": 1.0, "y": [1, 2, 3]}, **payload)
    key_b = compute_run_key(strategy_params={"y": [1, 2, 3], "x": 1.0}, **payload)
    assert key_a == key_b
    assert re.fullmatch(r"[0-9a-f]{64}", key_a)

    key_c = compute_run_key(
        strategy_params={"x": 1.0000000000000002, "y": [1, 2, 3]},
        **payload,
    )
    assert key_a == key_c


def test_migrations_files_present():
    files = migrations_pkg.list_migration_files()
    assert len(files) >= 2, "expected at least V0001 and V0002"

    pattern = re.compile(
        r"\b(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|GRANT|REVOKE|COMMENT|SET)\b",
        re.IGNORECASE,
    )
    for path in files:
        body = path.read_text(encoding="utf-8").strip()
        assert body, f"migration {path.name} is empty"
        assert pattern.search(body), f"migration {path.name} contains no DDL statements"

        # Sanity check: SQLite cannot parse JSONB/TIMESTAMPTZ/BIGSERIAL or
        # schema-qualified identifiers (meta.x, ops.x). If a file uses them,
        # skip the live-parse check; otherwise verify it parses cleanly via
        # SQLite's lexer.
        looks_pg_only = bool(
            re.search(r"\b(JSONB|TIMESTAMPTZ|BIGSERIAL)\b", body, re.IGNORECASE)
            or re.search(r"\b(meta|ops)\.[A-Za-z_]", body)
        )
        if looks_pg_only:
            continue
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(body)
        finally:
            conn.close()


def test_config_from_env(monkeypatch):
    for var in (
        "PG_DSN",
        "BACKTEST_METADATA_BACKEND",
        "BACKTEST_DATA_ROOT",
        "BACKTEST_SQLITE_PATH",
        "BACKTEST_ENGINE_KIND",
        "BACKTEST_EVENTS_MODE",
    ):
        monkeypatch.delenv(var, raising=False)

    cfg = AppConfig.from_env()
    assert cfg.pg_dsn is None
    assert cfg.metadata_backend == "sqlite"
    assert cfg.data_root == "data"
    assert cfg.sqlite_path == "klines.db"
    assert cfg.engine_kind == "python"
    assert cfg.events_mode == "lite"

    monkeypatch.setenv("PG_DSN", "postgresql://x:y@localhost/db")
    monkeypatch.setenv("BACKTEST_DATA_ROOT", "/tmp/data")
    monkeypatch.setenv("BACKTEST_ENGINE_KIND", "rust")
    monkeypatch.setenv("BACKTEST_EVENTS_MODE", "full")
    cfg2 = AppConfig.from_env()
    assert cfg2.pg_dsn == "postgresql://x:y@localhost/db"
    assert cfg2.metadata_backend == "pg"
    assert cfg2.data_root == "/tmp/data"
    assert cfg2.engine_kind == "rust"
    assert cfg2.events_mode == "full"

    monkeypatch.setenv("BACKTEST_METADATA_BACKEND", "sqlite")
    cfg3 = AppConfig.from_env()
    assert cfg3.metadata_backend == "sqlite"


def test_storage_facade_picks_sqlite_when_no_dsn(monkeypatch):
    from backtest.storage_facade import SqliteBackend, get_storage

    for var in ("PG_DSN", "BACKTEST_METADATA_BACKEND"):
        monkeypatch.delenv(var, raising=False)

    backend = get_storage()
    assert isinstance(backend, SqliteBackend)
    assert backend.kind == "sqlite"


def test_storage_facade_pg_requires_dsn(monkeypatch):
    from backtest.storage_facade import get_storage

    monkeypatch.delenv("PG_DSN", raising=False)
    monkeypatch.setenv("BACKTEST_METADATA_BACKEND", "pg")
    with pytest.raises(RuntimeError):
        get_storage()


@pytest.mark.skipif(os.getenv("PG_DSN") is None, reason="PG not available")
def test_pg_smoke(tmp_path):
    """Smoke test: only runs when PG_DSN points at a live, writable database.

    Applies migrations, inserts a fake run + metric via storage_pg, reads them
    back, and DELETEs them so the database is left in its prior state.
    """
    from backtest import storage_pg
    from backtest.migrations import apply_migrations

    dsn = os.environ["PG_DSN"]
    apply_migrations(dsn)

    paths = StoragePaths(data_root=str(tmp_path))
    run_id = storage_pg.create_run(
        dsn,
        strategy="test_phase0",
        symbol="TESTUSDT",
        interval="1m",
        start_ts=0,
        end_ts=60_000,
        initial_cash=1_000.0,
        fee_rate=0.0,
        slippage_bps=0.0,
        config={"smoke": True},
        engine_kind="python",
        engine_version="phase0-smoke",
        strategy_params={"alpha": 1.0},
        storage_paths=paths,
    )
    try:
        again = storage_pg.create_run(
            dsn,
            strategy="test_phase0",
            symbol="TESTUSDT",
            interval="1m",
            start_ts=0,
            end_ts=60_000,
            initial_cash=1_000.0,
            fee_rate=0.0,
            slippage_bps=0.0,
            config={"smoke": True},
            engine_kind="python",
            engine_version="phase0-smoke",
            strategy_params={"alpha": 1.0},
            storage_paths=paths,
        )
        assert again == run_id, "idempotent create_run must return the same run_id"

        storage_pg.persist_run_metrics(dsn, run_id=run_id, metrics={"profit": 1.5})
        descriptor = storage_pg.run_descriptor(dsn, run_id=run_id)
        assert descriptor is not None
        assert descriptor["symbol"] == "TESTUSDT"

        metrics = storage_pg.get_bt_run_metrics(dsn, run_id=run_id)
        assert metrics.get("profit") == 1.5
    finally:
        with storage_pg.transaction(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM meta.runs WHERE run_id = %s", (int(run_id),))
