from __future__ import annotations

import json
import os
import sqlite3

from backtest.data_integrity import interval_step_ms
from backtest.dataset_artifact import prepare_dataset_artifact, verify_dataset_artifact
from backtest.report_paths import dataset_report_dir
from db import init_db


def _seed_klines_with_gap(db_path: str, *, symbol: str, interval: str, base_ts: int) -> None:
    step = interval_step_ms(interval)
    rows = [
        (
            symbol,
            interval,
            base_ts + i * step,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            base_ts + i * step + step - 1,
            1.0,
            1,
            1.0,
            1.0,
            "",
        )
        for i in range(8)
        if i != 3
    ]
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            """
            INSERT INTO klines (symbol, interval, open_time, open, high, low, close, volume,
                close_time, quote_asset_volume, num_trades, taker_buy_base, taker_buy_quote, ignore_field)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def test_prepare_dataset_artifact_creates_generic_manifest(tmp_path):
    db_path = str(tmp_path / "klines.db")
    init_db(db_path)
    symbol = "XRPUSDT"
    interval = "1m"
    start_ts = 1_700_000_000_000
    _seed_klines_with_gap(db_path, symbol=symbol, interval=interval, base_ts=start_ts)

    output_base = str(tmp_path / "reports")
    artifact_name = "xrp_dataset_sample"
    payload = prepare_dataset_artifact(
        db_path=db_path,
        symbol=symbol,
        interval=interval,
        start_ts=start_ts,
        end_ts=start_ts + 7 * interval_step_ms(interval),
        output_base=output_base,
        artifact_name=artifact_name,
        prefer_parquet_cache=False,
    )

    artifact_dir = dataset_report_dir(output_base, artifact_name, ensure_manifest=False)
    manifest_path = os.path.join(artifact_dir, "manifest.json")
    assert os.path.exists(manifest_path)
    assert payload["window"]["symbol"] == symbol
    assert payload["integrity"]["row_count"] == 7
    assert payload["integrity"]["gap_count"] == 1
    assert payload["prepared_data"]["format"] == "jsonl"

    data_file = payload["prepared_data"]["files"][0]["path"]
    assert os.path.exists(data_file)


def test_verify_dataset_artifact_reports_missing_prepared_file(tmp_path):
    db_path = str(tmp_path / "klines.db")
    init_db(db_path)
    symbol = "BTCUSDT"
    interval = "1m"
    start_ts = 1_710_000_000_000
    _seed_klines_with_gap(db_path, symbol=symbol, interval=interval, base_ts=start_ts)

    output_base = str(tmp_path / "reports")
    artifact_name = "btc_dataset_verify"
    payload = prepare_dataset_artifact(
        db_path=db_path,
        symbol=symbol,
        interval=interval,
        start_ts=start_ts,
        end_ts=start_ts + 7 * interval_step_ms(interval),
        output_base=output_base,
        artifact_name=artifact_name,
        prefer_parquet_cache=False,
    )
    data_file = payload["prepared_data"]["files"][0]["path"]
    os.remove(data_file)

    artifact_dir = dataset_report_dir(output_base, artifact_name, ensure_manifest=False)
    manifest_path = os.path.join(artifact_dir, "manifest.json")
    result = verify_dataset_artifact(manifest_path)
    assert result["status"] == "invalid"
    assert result["missing_files"]

    with open(manifest_path, "r", encoding="utf-8") as fh:
        loaded = json.load(fh)
    assert loaded["artifact_kind"] == "dataset_window"
