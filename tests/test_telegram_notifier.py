"""Tests for the Telegram notifier module.

These tests exercise:
 - build_daily_report() with real DB data
 - send_message() with a mocked requests.post
 - Graceful degradation when env vars are missing
 - Notifier start/stop lifecycle
"""
from __future__ import annotations

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.models import (
    BotState,
    Event,
    EventKind,
    EventLevel,
    EventSource,
)
from backtest.agartha_cluster.notifier import (
    TelegramNotifier,
    _seconds_until_next,
    try_create_notifier,
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    """Fresh in-memory-like DB for each test."""
    db_path = str(tmp_path / "test_notifier.db")
    db = ClusterDB(db_path)
    db.init_schema()
    yield db
    db.close()


def _seed_bots_and_events(db: ClusterDB):
    """Insert a few bots and events so the report has data."""
    from datetime import datetime, timezone
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")

    # Closed win
    bot1 = db.create_bot(
        symbol="TESTUSDT",
        capital_usdt=10.0,
        params_snapshot={"trailing_stop_pct": 0.02},
        correlation_id="agc-1",
        state=BotState.QUEUED,
    )
    db.update_bot(bot1, state=BotState.CLOSED_WIN.value, realized_pnl_usdt=5.0, closed_at=today)

    # Closed loss
    bot2 = db.create_bot(
        symbol="FOOUSDT",
        capital_usdt=10.0,
        params_snapshot={"trailing_stop_pct": 0.02},
        correlation_id="agc-2",
        state=BotState.QUEUED,
    )
    db.update_bot(bot2, state=BotState.CLOSED_LOSS.value, realized_pnl_usdt=-2.0, closed_at=today)

    # In position
    bot3 = db.create_bot(
        symbol="BARUSDT",
        capital_usdt=10.0,
        params_snapshot={"trailing_stop_pct": 0.02},
        correlation_id="agc-3",
        state=BotState.IN_POSITION,
    )

    # Service run (online)
    db.start_service_run(mode="live", pid=12345, host="test-host", version="0.1.5")

    # Critical event
    db.log_event(Event(
        ts_ms=int(time.time() * 1000),
        source=EventSource.SERVICE,
        level=EventLevel.CRITICAL,
        kind=EventKind.SERVICE_PREVIOUS_CRASH_DETECTED,
        payload={"test": True},
    ))

    # Resource metric
    db.insert_resource_metric(
        ts_ms=int(time.time() * 1000),
        proc_cpu_pct=2.5,
        proc_ram_mb=48.0,
        host_cpu_pct=12.0,
        host_ram_pct=55.0,
        disk_used_gb=30.0,
        disk_free_gb=70.0,
        disk_pct=30.0,
    )


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestBuildDailyReport:
    def test_report_contains_expected_sections(self, db):
        _seed_bots_and_events(db)
        notifier = TelegramNotifier("FAKE_TOKEN", "123", db, cluster_version="0.1.5", mode="live")
        report = notifier.build_daily_report()

        assert "Reporte Diario" in report
        assert "0.1.5" in report
        assert "live" in report
        assert "USDT" in report
        assert "EN LÍNEA" in report
        assert "CPU" in report

    def test_report_with_empty_db(self, db):
        notifier = TelegramNotifier("FAKE_TOKEN", "123", db, cluster_version="0.1.5", mode="dry-run")
        report = notifier.build_daily_report()

        # Should still produce a valid report, even with zero bots
        assert "Reporte Diario" in report
        assert "SIN PROCESO ACTIVO" in report

    def test_report_shows_pnl(self, db):
        _seed_bots_and_events(db)
        notifier = TelegramNotifier("FAKE_TOKEN", "123", db, cluster_version="0.1.5")
        report = notifier.build_daily_report()

        # Net PnL should be +3.0 (5 - 2)
        assert "3.0000" in report

    def test_report_shows_alerts(self, db):
        _seed_bots_and_events(db)
        notifier = TelegramNotifier("FAKE_TOKEN", "123", db, cluster_version="0.1.5")
        report = notifier.build_daily_report()

        assert "service_previous_crash_detected" in report


class TestSendMessage:
    @patch("backtest.agartha_cluster.notifier._http.post")
    def test_send_message_success(self, mock_post, db):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        notifier = TelegramNotifier("TOKEN123", "CHAT456", db)
        result = notifier.send_message("Hello!")

        assert result is True
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "TOKEN123" in call_args[0][0]  # URL contains token
        assert call_args[1]["json"]["chat_id"] == "CHAT456"
        assert call_args[1]["json"]["text"] == "Hello!"

    @patch("backtest.agartha_cluster.notifier._http.post")
    def test_send_message_api_error(self, mock_post, db):
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"
        mock_post.return_value = mock_resp

        notifier = TelegramNotifier("TOKEN123", "CHAT456", db)
        result = notifier.send_message("Hello!")

        assert result is False

    @patch("backtest.agartha_cluster.notifier._http.post", side_effect=Exception("Network down"))
    def test_send_message_network_error(self, mock_post, db):
        notifier = TelegramNotifier("TOKEN123", "CHAT456", db)
        result = notifier.send_message("Hello!")

        assert result is False


class TestSendAlert:
    @patch("backtest.agartha_cluster.notifier._http.post")
    def test_alert_contains_emoji(self, mock_post, db):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        notifier = TelegramNotifier("TOKEN", "CHAT", db)
        notifier.send_alert("Server is down")

        text = mock_post.call_args[1]["json"]["text"]
        assert "🚨" in text
        assert "Server is down" in text


class TestTryCreateNotifier:
    def test_returns_none_without_env(self, db):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": ""}, clear=False):
            result = try_create_notifier(db)
            assert result is None

    def test_returns_notifier_with_env(self, db):
        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "tok", "TELEGRAM_CHAT_ID": "123"}, clear=False):
            result = try_create_notifier(db, cluster_version="0.1.5", mode="live")
            assert isinstance(result, TelegramNotifier)


class TestScheduling:
    def test_seconds_until_next_is_positive(self):
        secs = _seconds_until_next(8)
        assert secs > 0
        assert secs <= 86_400 + 60  # at most 24h + guard

    @patch("backtest.agartha_cluster.notifier._http.post")
    def test_start_sends_startup_message(self, mock_post, db):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        notifier = TelegramNotifier("TOKEN", "CHAT", db, cluster_version="0.1.5", mode="dry-run")
        notifier.start()

        # Should have sent a startup ping
        assert mock_post.call_count >= 1
        text = mock_post.call_args[1]["json"]["text"]
        assert "iniciado" in text

        notifier.stop()

    def test_stop_cancels_timer(self, db):
        notifier = TelegramNotifier("TOKEN", "CHAT", db)

        # Manually set a timer
        notifier._timer = threading.Timer(9999, lambda: None)
        notifier._timer.daemon = True
        notifier._timer.start()

        notifier.stop()
        assert notifier._stopped is True
        assert notifier._timer is None


class TestDailyReport:
    @patch("backtest.agartha_cluster.notifier._http.post")
    def test_send_daily_report_calls_send_message(self, mock_post, db):
        _seed_bots_and_events(db)
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_post.return_value = mock_resp

        notifier = TelegramNotifier("TOKEN", "CHAT", db, cluster_version="0.1.5")
        notifier.send_daily_report()

        # Should have sent the report and rescheduled
        assert mock_post.call_count >= 1
        text = mock_post.call_args[1]["json"]["text"]
        assert "Reporte Diario" in text

        # Timer should be rescheduled
        assert notifier._timer is not None
        notifier.stop()
