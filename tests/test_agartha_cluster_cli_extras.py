"""Tests for `load-universe --from-binance` and `import-params`.

Network is mocked. Optuna studies are real (in-memory SQLite via Optuna's
own backend), so we cover both the optuna.db code path and the
trial_to_run.json fallback path.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from backtest.agartha_cluster import cli as cluster_cli
from backtest.agartha_cluster.cluster_db import ClusterDB


# ---------------------------------------------------------------------------
# load-universe --from-binance
# ---------------------------------------------------------------------------


_FAKE_TOKEN_LIST = [
    {
        "symbol": "FOOUSDT",
        "alphaId": "ALPHA_111USDT",
        "liquidity": 12345.6,
        "holders": 2000,
        "offline": False,
        "offsell": False,
        "chainName": "BSC",
    },
    {
        "symbol": "BARUSDT",
        "alphaId": "ALPHA_222USDT",
        "liquidity": 999.0,
        "holders": 50,
        "offline": True,            # filtered out by default
        "offsell": False,
        "chainName": "ETH",
    },
    {
        "symbol": "BAZUSDT",
        "alphaId": "ALPHA_333USDT",
        "liquidity": None,
        "holders": None,
        "offline": False,
        "offsell": True,            # filtered out by default
        "chainName": "BSC",
    },
]


class _FakeDownloader:
    def get_alpha_token_list(self):
        return list(_FAKE_TOKEN_LIST)


@pytest.fixture
def fake_binance(monkeypatch):
    import binance_hist_downloader  # noqa: F401  (ensure module is importable)

    monkeypatch.setattr(
        cluster_cli, "_fetch_alpha_token_list_from_binance",
        lambda *, include_offline, include_offsell: [
            t for t in _FAKE_TOKEN_LIST
            if (include_offline or not t.get("offline"))
            and (include_offsell or not t.get("offsell"))
        ],
    )
    yield


def test_load_universe_from_binance_default_filters(tmp_path, fake_binance):
    db_path = tmp_path / "cluster.db"
    rc = cluster_cli.main([
        "--db", str(db_path),
        "--log-dir", str(tmp_path / "logs"),
        "load-universe", "--from-binance",
    ])
    assert rc == 0
    db = ClusterDB(str(db_path))
    rows = db.list_universe(status="eligible")
    syms = sorted(r["symbol"] for r in rows)
    assert syms == ["FOOUSDT"]  # BAR offline, BAZ offsell -> filtered
    foo = next(r for r in rows if r["symbol"] == "FOOUSDT")
    assert foo["alpha_id"] == "ALPHA_111USDT"
    assert float(foo["liquidity_usd"]) == pytest.approx(12345.6)
    assert int(foo["holders"]) == 2000
    md = json.loads(foo["metadata_json"])
    assert md.get("chainName") == "BSC"
    db.close()


def test_load_universe_from_binance_include_offline(tmp_path, fake_binance):
    db_path = tmp_path / "cluster.db"
    rc = cluster_cli.main([
        "--db", str(db_path),
        "--log-dir", str(tmp_path / "logs"),
        "load-universe", "--from-binance", "--include-offline", "--include-offsell",
    ])
    assert rc == 0
    db = ClusterDB(str(db_path))
    syms = sorted(r["symbol"] for r in db.list_universe())
    assert syms == ["BARUSDT", "BAZUSDT", "FOOUSDT"]
    db.close()


def test_load_universe_from_binance_limit_and_export(tmp_path, fake_binance):
    db_path = tmp_path / "cluster.db"
    export = tmp_path / "tokens.json"
    rc = cluster_cli.main([
        "--db", str(db_path),
        "--log-dir", str(tmp_path / "logs"),
        "load-universe", "--from-binance",
        "--limit", "1",
        "--export-json", str(export),
    ])
    assert rc == 0
    assert export.exists()
    payload = json.loads(export.read_text(encoding="utf-8"))
    assert isinstance(payload, list) and len(payload) >= 1
    db = ClusterDB(str(db_path))
    rows = db.list_universe()
    assert len(rows) == 1
    db.close()


def test_load_universe_requires_a_source(tmp_path, capsys):
    db_path = tmp_path / "cluster.db"
    with pytest.raises(SystemExit):
        # argparse mutex: missing both flags exits 2 directly via the parser.
        cluster_cli.main([
            "--db", str(db_path),
            "--log-dir", str(tmp_path / "logs"),
            "load-universe",
        ])


def test_load_universe_from_json_path(tmp_path):
    db_path = tmp_path / "cluster.db"
    src = tmp_path / "universe.json"
    src.write_text(
        json.dumps([
            {
                "symbol": "abcusdt",  # lowercase -> normalised
                "alpha_id": "ALPHA_77USDT",
                "liquidity": 500.0,
                "holders": 100,
            }
        ]),
        encoding="utf-8",
    )
    rc = cluster_cli.main([
        "--db", str(db_path),
        "--log-dir", str(tmp_path / "logs"),
        "load-universe", "--from-json", str(src),
    ])
    assert rc == 0
    db = ClusterDB(str(db_path))
    rows = db.list_universe()
    assert [r["symbol"] for r in rows] == ["ABCUSDT"]
    assert float(rows[0]["liquidity_usd"]) == 500.0
    db.close()


# ---------------------------------------------------------------------------
# import-params
# ---------------------------------------------------------------------------


def _make_optuna_study(tmp_path: Path, study_name: str) -> Path:
    """Create a small Optuna study and return the SQLite path."""
    optuna = pytest.importorskip("optuna")
    studies_dir = tmp_path / "reports" / "entregables" / "studies" / study_name
    studies_dir.mkdir(parents=True, exist_ok=True)
    db_path = studies_dir / "optuna.db"
    storage_url = f"sqlite:///{db_path.resolve().as_posix()}"
    study = optuna.create_study(
        study_name=study_name, storage=storage_url, direction="maximize"
    )

    def objective(trial):
        t = trial.suggest_float("trailing_stop_pct", 5.0, 50.0)
        a = trial.suggest_float("activation_profit_pct", 0.0, 50.0)
        b = trial.suggest_float("breakeven_lock_pct", 0.0, 100.0)
        o = trial.suggest_float("entry_limit_offset_pct", 0.0, 5.0)
        return t - a / 10.0 + b / 100.0 - o

    study.optimize(objective, n_trials=5, show_progress_bar=False)
    return db_path


def test_import_params_single_via_optuna(tmp_path):
    pytest.importorskip("optuna")
    study_name = "agartha_test_FOO_15m"
    _make_optuna_study(tmp_path, study_name)
    db_path = tmp_path / "cluster.db"
    rc = cluster_cli.main([
        "--db", str(db_path),
        "--log-dir", str(tmp_path / "logs"),
        "import-params",
        "--symbol", "FOOUSDT",
        "--study", study_name,
        "--root", str(tmp_path / "reports"),
    ])
    assert rc == 0
    db = ClusterDB(str(db_path))
    params = db.get_symbol_params("FOOUSDT")
    assert params is not None
    assert params.trailing_stop_pct > 0
    assert params.entry_limit_offset_pct >= 0
    assert params.study_trial_id is not None
    db.close()


def test_import_params_batch_via_optuna(tmp_path):
    pytest.importorskip("optuna")
    s1, s2 = "agartha_FOO_test", "agartha_BAR_test"
    _make_optuna_study(tmp_path, s1)
    _make_optuna_study(tmp_path, s2)
    batch = tmp_path / "batch.json"
    batch.write_text(
        json.dumps([
            {"symbol": "FOOUSDT", "study": s1},
            {"symbol": "BARUSDT", "study": s2},
        ]),
        encoding="utf-8",
    )
    db_path = tmp_path / "cluster.db"
    rc = cluster_cli.main([
        "--db", str(db_path),
        "--log-dir", str(tmp_path / "logs"),
        "import-params",
        "--batch-json", str(batch),
        "--root", str(tmp_path / "reports"),
    ])
    assert rc == 0
    db = ClusterDB(str(db_path))
    assert db.get_symbol_params("FOOUSDT") is not None
    assert db.get_symbol_params("BARUSDT") is not None
    db.close()


def test_import_params_fallback_trial_to_run_json(tmp_path):
    """When optuna.db missing but trial_to_run.json exists, use the JSON fallback."""
    study_name = "agartha_fallback_FOO"
    studies_dir = tmp_path / "reports" / "entregables" / "studies" / study_name
    studies_dir.mkdir(parents=True, exist_ok=True)
    (studies_dir / "trial_to_run.json").write_text(
        json.dumps({
            "study_name": study_name,
            "best_trial_number": 42,
            "best_value": 1234.5,
            "best_params": {
                "trailing_stop_pct": 28.0,
                "activation_profit_pct": 5.0,
                "breakeven_lock_pct": 0.0,
                "entry_limit_offset_pct": 1.5,
            },
        }),
        encoding="utf-8",
    )
    db_path = tmp_path / "cluster.db"
    rc = cluster_cli.main([
        "--db", str(db_path),
        "--log-dir", str(tmp_path / "logs"),
        "import-params",
        "--symbol", "FOOUSDT",
        "--study", study_name,
        "--root", str(tmp_path / "reports"),
    ])
    assert rc == 0
    db = ClusterDB(str(db_path))
    p = db.get_symbol_params("FOOUSDT")
    assert p is not None
    assert p.trailing_stop_pct == 28.0
    assert p.activation_profit_pct == 5.0
    assert p.entry_limit_offset_pct == 1.5
    assert p.study_trial_id == "42"
    assert p.study_equity_pct == 1234.5
    db.close()


def test_import_params_missing_study_fails_gracefully(tmp_path):
    db_path = tmp_path / "cluster.db"
    rc = cluster_cli.main([
        "--db", str(db_path),
        "--log-dir", str(tmp_path / "logs"),
        "import-params",
        "--symbol", "FOOUSDT",
        "--study", "does_not_exist",
        "--root", str(tmp_path / "reports"),
    ])
    assert rc == 1  # 1 fail, no ok


def test_import_params_requires_symbol_and_study_or_batch(tmp_path):
    db_path = tmp_path / "cluster.db"
    rc = cluster_cli.main([
        "--db", str(db_path),
        "--log-dir", str(tmp_path / "logs"),
        "import-params",
    ])
    assert rc == 2


def test_report_resources_cli(tmp_path, capsys):
    db_path = tmp_path / "cluster.db"
    db = ClusterDB(str(db_path))
    db.init_schema()

    # Check behavior when no metrics exist
    rc = cluster_cli.main([
        "--db", str(db_path),
        "--log-dir", str(tmp_path / "logs"),
        "report-resources",
        "--days", "1",
    ])
    assert rc == 0
    captured = capsys.readouterr()
    assert "No hay métricas registradas" in captured.out

    # Insert fake metrics
    db.insert_resource_metric(
        ts_ms=int((time.time() - 3600) * 1000),
        proc_cpu_pct=15.0,
        proc_ram_mb=100.0,
        host_cpu_pct=40.0,
        host_ram_pct=50.0,
        disk_used_gb=10.0,
        disk_free_gb=100.0,
        disk_pct=9.0,
    )
    db.insert_resource_metric(
        ts_ms=int(time.time() * 1000),
        proc_cpu_pct=25.0,
        proc_ram_mb=200.0,
        host_cpu_pct=60.0,
        host_ram_pct=70.0,
        disk_used_gb=12.0,
        disk_free_gb=98.0,
        disk_pct=11.0,
    )
    db.close()

    rc2 = cluster_cli.main([
        "--db", str(db_path),
        "--log-dir", str(tmp_path / "logs"),
        "report-resources",
        "--days", "1",
    ])
    assert rc2 == 0
    captured2 = capsys.readouterr()
    # Check for basic table values and recommendation headers
    assert "REPORTE DE CONSUMO DE RECURSOS" in captured2.out
    assert "CPU Proceso (%)" in captured2.out
    assert "CPU Host (%)" in captured2.out
    assert "RAM Proceso (MB)" in captured2.out
    assert "RAM Host (%)" in captured2.out
    assert "DISEÑO Y RECOMENDACIÓN DE SERVICIO CLOUD" in captured2.out
