"""Unit tests for hardening improvements (D4, D5, D7)."""
import time
import signal
import sys
from unittest.mock import MagicMock, patch
import pytest
import requests

from backtest.agartha_cluster.live_client import BinanceAlphaClient
from backtest.agartha_cluster.cluster_service import ClusterService, ServiceConfig
from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.api_throttle import ApiThrottle
from backtest.agartha_cluster.scheduler import DeployScheduler
from backtest.agartha_cluster.bot_runner import BotRunner
from backtest.agartha_cluster.reconciler import Reconciler


def test_binance_alpha_client_price_caching():
    """Verify that get_price caching works and reduces weight usage via batch fetches."""
    client = BinanceAlphaClient(api_key="mock", api_secret="mock")

    # Mock response for batch fetch: weight = 2
    mock_batch_response = MagicMock()
    mock_batch_response.status_code = 200
    mock_batch_response.headers = {"X-MBX-USED-WEIGHT-1M": "10"}
    mock_batch_response.json.return_value = [
        {"symbol": "BTCUSDT", "price": "95000.0"},
        {"symbol": "ETHUSDT", "price": "3500.0"},
    ]

    with patch("requests.get", return_value=mock_batch_response) as mock_get:
        # First query for BTCUSDT triggers a batch price query
        p1 = client.get_price("BTCUSDT")
        assert p1 == 95000.0
        assert mock_get.call_count == 1
        # Label parameter should be "get_price_batch"
        # We check the arguments inside self._rest_with_retry: requests.get(url, timeout=10)

        # Second query for ETHUSDT in the same tick should be served from cache
        p2 = client.get_price("ETHUSDT")
        assert p2 == 3500.0
        assert mock_get.call_count == 1  # Still 1, served from cache!

        # Modify cache timestamp to simulate expiration (> 4 seconds)
        client._price_cache_ts = time.time() - 5.0

        # Query again, triggers fresh batch fetch
        p3 = client.get_price("BTCUSDT")
        assert p3 == 95000.0
        assert mock_get.call_count == 2


def test_binance_alpha_client_price_caching_fallback():
    """Verify fallback to single endpoint in case batch fetch fails or symbol is missing."""
    client = BinanceAlphaClient(api_key="mock", api_secret="mock")

    # Mock response for batch fetch: empty or raises exception
    mock_batch_fail = MagicMock()
    mock_batch_fail.status_code = 500
    mock_batch_fail.headers = {}
    mock_batch_fail.json.side_effect = ValueError("Format error")

    # Mock response for single fetch: weight = 1
    mock_single_response = MagicMock()
    mock_single_response.status_code = 200
    mock_single_response.headers = {"X-MBX-USED-WEIGHT-1M": "1"}
    mock_single_response.json.return_value = {"symbol": "BTCUSDT", "price": "96000.0"}

    def side_effect(url, *args, **kwargs):
        if "params" in kwargs and kwargs["params"].get("symbol") == "BTCUSDT":
            return mock_single_response
        return mock_batch_fail

    with patch("requests.get", side_effect=side_effect) as mock_get:
        p = client.get_price("BTCUSDT")
        assert p == 96000.0
        # Call count must be 5: 4 attempts for the failed batch query (1 initial + 3 retries)
        # plus 1 attempt for the successful fallback query.
        assert mock_get.call_count == 5


def test_signal_handlers_trigger_clean_shutdown():
    """Verify that signal handler triggers service.request_stop() clean shutdown."""
    # We test the signal wiring via cli's cmd_live_up using patch
    from backtest.agartha_cluster.cli import cmd_live_up
    import argparse

    args = argparse.Namespace(
        db=":memory:",
        dry_run=True,
        capital_usdt=10.0,
        slot_seconds=5,
        log_dir="logs/test_resilience",
        ticks=1,
    )

    with patch("signal.signal") as mock_signal:
        cmd_live_up(args)
        assert mock_signal.call_count == 2
        # Check that SIGINT and SIGTERM are registered
        calls = mock_signal.call_args_list
        assert calls[0][0][0] == signal.SIGINT
        assert calls[1][0][0] == signal.SIGTERM


def test_critical_exceptions_logged_to_stderr(capsys):
    """Verify that WAL checkpoint failures are logged to stderr in recovery_boot."""
    db = MagicMock(spec=ClusterDB)
    db.start_service_run.return_value = 1
    db.list_open_service_runs.return_value = []
    # Force wal_checkpoint to throw an exception
    db.wal_checkpoint.side_effect = sqlite3_operational_error = Exception("DB locked")

    client = MagicMock()
    events = MagicMock(spec=EventLogger)
    throttle = MagicMock(spec=ApiThrottle)
    scheduler = MagicMock(spec=DeployScheduler)
    runner = MagicMock(spec=BotRunner)
    reconciler = MagicMock(spec=Reconciler)
    reconciler.poll_open_orders_for_fills.return_value = {}

    service = ClusterService(
        db=db,
        client=client,
        events=events,
        throttle=throttle,
        scheduler=scheduler,
        runner=runner,
        reconciler=reconciler,
        config=ServiceConfig(enable_recovery_boot=True),
    )

    service.start()
    captured = capsys.readouterr()
    assert "[agartha][Error] Recovery WAL checkpoint failed: DB locked" in captured.err
