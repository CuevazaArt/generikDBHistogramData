"""Tests for the library system (manifests, loader, scaffold, data provider)."""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import backtest.library as lib
from backtest import registry
from backtest.data_provider import ParquetDataProvider, SQLiteDataProvider, get_data_provider


MIGRATED_BOTS = (
    "sma_cross",
    "dorothy",
    "dorothy_legacy",
    "elphaba",
    "ha_trend",
    "masha",
    "thusnelda",
    "louise",
    "louise_lucky",
    "anti_louise",
    "anti_louise_lucky",
)

REFERENCE_BOTS = (
    "dorothy_live_reference",
    "masha_live_reference",
    "thusnelda_live_reference",
)


def test_list_entries_includes_all_migrated_and_reference_bots():
    entries = {e.name: e for e in lib.list_entries(kind="bot")}
    for name in MIGRATED_BOTS + REFERENCE_BOTS:
        assert name in entries, f"library entry missing: {name}"
    for name in REFERENCE_BOTS:
        assert entries[name].is_reference_only(), f"{name} should be reference_only"


def test_load_entry_dorothy_resolves_and_instantiates_defaults():
    entry = lib.load_entry("dorothy")
    cls = lib.resolve_entry_point(entry)
    assert cls.__name__ == "DorothyHubStrategy"
    defaults = entry.manifest.get("default_params") or {}
    instance = cls(**defaults)
    assert instance.profit_factor == pytest.approx(0.05)
    assert instance.max_rungs == 5


def test_register_with_strategy_registry_makes_get_strategy_resolve():
    lib.register_with_strategy_registry()
    assert registry.get_strategy("dorothy").__name__ == "DorothyHubStrategy"
    assert registry.get_strategy("dorothy_hub").__name__ == "DorothyHubStrategy"
    assert registry.get_strategy("elphaba").__name__ == "ElphabaHubStrategy"
    assert registry.get_strategy("louise_lucky").__name__ == "LouiseLuckyStrategy"


def test_validate_entry_flags_broken_manifest(tmp_path):
    bad_dir = lib.LIBRARY_ROOT / "workspace" / "_broken_entry"
    if bad_dir.exists():
        shutil.rmtree(bad_dir)
    bad_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = bad_dir / "manifest.yaml"
    manifest_path.write_text(
        "schema_version: 1\nname: _broken_entry\nkind: bot\nversion: 0.0.1\n",
        encoding="utf-8",
    )
    try:
        result = lib.validate_entry("_broken_entry", include_workspace=True)
        assert not result["ok"]
        assert any("entry_point" in err for err in result["errors"])
    finally:
        shutil.rmtree(bad_dir, ignore_errors=True)


def test_scaffold_and_publish_workspace_draft():
    draft_name = "_test_my_draft_for_pytest"
    workspace_dir = lib.LIBRARY_ROOT / "workspace" / draft_name
    published_dir = lib.LIBRARY_ROOT / "bots" / draft_name
    if workspace_dir.exists():
        shutil.rmtree(workspace_dir)
    if published_dir.exists():
        shutil.rmtree(published_dir)
    try:
        created = lib.scaffold_entry(draft_name, kind="bot", workspace=True)
        assert created == workspace_dir
        assert (workspace_dir / "manifest.yaml").exists()
        assert (workspace_dir / "strategy.py").exists()
        assert (workspace_dir / "notes.md").exists()
        assert (workspace_dir / "presets" / "default.yaml").exists()

        moved = lib.publish_entry(draft_name)
        assert moved == published_dir
        assert published_dir.exists()
        assert not workspace_dir.exists()
        index_path = lib.refresh_index()
        assert index_path.exists()
    finally:
        for path in (workspace_dir, published_dir):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        lib.refresh_index()


def test_list_presets_and_load_preset_for_dorothy():
    presets = lib.list_presets("dorothy")
    assert "default" in presets
    params = lib.load_preset("dorothy", "default")
    assert params["profit_factor"] == pytest.approx(0.05)


def test_refresh_index_writes_json():
    path = lib.refresh_index()
    assert path.exists()
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["counts"]["total"] >= len(MIGRATED_BOTS) + len(REFERENCE_BOTS)
    assert payload["counts"]["reference_only"] >= len(REFERENCE_BOTS)


def test_data_provider_parquet_round_trip():
    parquet_root = Path("data") / "klines"
    target = (
        parquet_root
        / "symbol=BTCUSDT"
        / "interval=1h"
        / "year=2024"
        / "month=01"
        / "part-000.parquet"
    )
    if not target.exists():
        pytest.skip(f"parquet partition not present: {target}")

    provider = get_data_provider(backend="parquet", klines_root=str(parquet_root))
    assert isinstance(provider, ParquetDataProvider)
    assert "BTCUSDT" in provider.list_symbols()
    assert "1h" in provider.list_intervals("BTCUSDT")
    rows = provider.load_candles(
        symbol="BTCUSDT",
        interval="1h",
        start_ts=1704067200000,
        end_ts=1706742000000,
    )
    assert len(rows) == 744
    for row in rows[:3]:
        assert row["symbol"] == "BTCUSDT"
        assert row["interval"] == "1h"
        for col in ("open", "high", "low", "close", "volume", "open_time"):
            assert col in row


def test_data_provider_factory_defaults_to_parquet_when_manifest_exists():
    manifest_path = Path("data") / "klines" / "_manifest.json"
    if not manifest_path.exists():
        pytest.skip("parquet manifest not present in workspace")
    provider = get_data_provider()
    assert isinstance(provider, ParquetDataProvider)


def test_sqlite_data_provider_lists_symbols_when_db_present():
    db_path = Path("klines.db")
    if not db_path.exists():
        pytest.skip("klines.db not present in workspace")
    provider = SQLiteDataProvider(db_path=str(db_path))
    symbols = provider.list_symbols()
    assert isinstance(symbols, list)
