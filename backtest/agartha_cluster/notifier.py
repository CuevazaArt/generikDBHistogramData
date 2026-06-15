"""Telegram notifier for the Agartha cluster.

Sends a daily status digest and immediate critical alerts via the
Telegram Bot API.  Uses ``requests`` (already a project dependency)
directly — no extra packages required.

The notifier runs on a daemon ``threading.Timer`` so it never blocks
the main trading loop.  If ``TELEGRAM_BOT_TOKEN`` or ``TELEGRAM_CHAT_ID``
are missing, the notifier disables itself silently with a log line.
"""
# ruff: noqa: E501
from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import requests as _http

from backtest.agartha_cluster.models import EventKind, EventLevel

if TYPE_CHECKING:
    from backtest.agartha_cluster.cluster_db import ClusterDB

# Telegram API
_TG_API = "https://api.telegram.org/bot{token}/sendMessage"

# Default daily report hour (UTC).  08:00 UTC ≈ 02:00 CST.
_DEFAULT_REPORT_HOUR_UTC = 8


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _seconds_until_next(hour_utc: int) -> float:
    """Seconds from *now* until the next occurrence of ``hour_utc:00``."""
    now = _utc_now()
    target = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if target <= now:
        # already past today → schedule for tomorrow
        target = target.replace(day=target.day + 1)
    diff = (target - now).total_seconds()
    # Guard against DST edge cases / negative drift
    return max(diff, 60.0)


class TelegramNotifier:
    """Lightweight Telegram alerter integrated with :class:`ClusterDB`.

    Parameters
    ----------
    bot_token : str
        Telegram Bot API token from ``@BotFather``.
    chat_id : str
        Numeric chat / group ID where messages are sent.
    db : ClusterDB
        Database handle used to build the daily report.
    report_hour_utc : int
        Hour (0-23) at which the daily digest fires (default 8 → 08:00 UTC).
    cluster_version : str
        Version string included in the report header.
    mode : str
        Running mode (``live`` / ``dry-run``).
    """

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        db: "ClusterDB",
        *,
        report_hour_utc: int = _DEFAULT_REPORT_HOUR_UTC,
        cluster_version: str = "",
        mode: str = "live",
    ):
        self._token = bot_token
        self._chat_id = chat_id
        self._db = db
        self._report_hour = report_hour_utc
        self._version = cluster_version
        self._mode = mode
        self._timer: threading.Timer | None = None
        self._stopped = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def send_message(self, text: str, *, parse_mode: str = "HTML") -> bool:
        """Post *text* to the configured chat.  Returns ``True`` on success."""
        url = _TG_API.format(token=self._token)
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        try:
            resp = _http.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                return True
            print(
                f"[agartha][telegram] API returned {resp.status_code}: {resp.text[:200]}",
                file=sys.stderr, flush=True,
            )
            return False
        except Exception as exc:
            print(
                f"[agartha][telegram] send_message failed: {exc}",
                file=sys.stderr, flush=True,
            )
            return False

    def send_alert(self, message: str) -> None:
        """Send an immediate critical alert."""
        text = f"🚨 <b>Agartha Cluster — Alerta Crítica</b>\n\n{message}"
        self.send_message(text)

    # ------------------------------------------------------------------
    # Daily report
    # ------------------------------------------------------------------

    def build_daily_report(self) -> str:
        """Query the DB and compose the daily digest string."""
        now = _utc_now()
        ts_24h_ago = int((now.timestamp() - 86_400) * 1000)

        # -- Bot stats (last 24 h) --
        all_bots = self._db.list_bots()
        wins, losses, in_pos, awaiting = 0, 0, 0, 0
        pnl_win, pnl_loss = 0.0, 0.0
        today_utc = now.strftime("%Y-%m-%d")

        for b in all_bots:
            state = b.state.value

            if state == "closed_win":
                # closed_at may be "YYYY-MM-DD" or "YYYY-MM-DD HH:MM:SS"
                if b.closed_at and b.closed_at[:10] >= today_utc:
                    wins += 1
                    pnl_win += b.realized_pnl_usdt or 0.0
            elif state == "closed_loss":
                if b.closed_at and b.closed_at[:10] >= today_utc:
                    losses += 1
                    pnl_loss += b.realized_pnl_usdt or 0.0
            elif state == "in_position":
                in_pos += 1
            elif state in (
                "placing_entry", "awaiting_entry_fill",
                "placing_exit", "awaiting_exit_fill", "queued",
            ):
                awaiting += 1

        pnl_net = pnl_win + pnl_loss  # losses are negative

        # -- Pending critical events (last 24 h) --
        critical_events = self._db.query_events(
            level=EventLevel.CRITICAL, since_ms=ts_24h_ago, limit=10,
        )
        manual_events = self._db.query_events(
            kind=EventKind.NEEDS_MANUAL_ACTION, since_ms=ts_24h_ago, limit=10,
        )
        pending_alerts: list[str] = []
        seen_kinds: set[str] = set()
        for ev in list(critical_events) + list(manual_events):
            kind = ev["kind"]
            if kind not in seen_kinds:
                seen_kinds.add(kind)
                pending_alerts.append(f"  • {kind}")

        alerts_block = "\n".join(pending_alerts) if pending_alerts else "  Ninguna ✅"

        # -- Resource snapshot (latest) --
        metrics = self._db.get_resource_metrics(limit=1)
        if metrics:
            m = metrics[-1]
            res_line = (
                f"  CPU: {m['proc_cpu_pct']:.1f}% "
                f"| RAM: {m['proc_ram_mb']:.0f} MB "
                f"| Disco: {m['disk_pct']:.0f}%"
            )
        else:
            res_line = "  Sin datos de recursos"

        # -- Service run status --
        open_runs = self._db.list_open_service_runs()
        if open_runs:
            status_emoji = "🟢"
            status_text = "EN LÍNEA y operando"
        else:
            status_emoji = "🔴"
            status_text = "SIN PROCESO ACTIVO"

        # -- Compose message --
        pnl_sign = "+" if pnl_net >= 0 else ""
        report = (
            f"🟢 <b>Agartha Cluster — Reporte Diario</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📅 {now.strftime('%Y-%m-%d %H:%M')} UTC\n"
            f"🖥️ Versión: {self._version} | Modo: {self._mode}\n"
            f"\n"
            f"📊 <b>Bots (hoy):</b>\n"
            f"  ✅ Cerrados ganancia: {wins} (PnL: +{pnl_win:.4f} USDT)\n"
            f"  ❌ Cerrados pérdida:  {losses} (PnL: {pnl_loss:.4f} USDT)\n"
            f"  🔄 En posición:       {in_pos}\n"
            f"  ⏳ En tránsito:       {awaiting}\n"
            f"  📋 <b>Neto hoy: {pnl_sign}{pnl_net:.4f} USDT</b>\n"
            f"\n"
            f"⚠️ <b>Alertas pendientes:</b>\n"
            f"{alerts_block}\n"
            f"\n"
            f"💻 <b>Recursos:</b>\n"
            f"{res_line}\n"
            f"\n"
            f"{status_emoji} El cluster está <b>{status_text}</b>."
        )
        return report

    def send_daily_report(self) -> None:
        """Build and send the daily report, then reschedule for tomorrow."""
        if self._stopped:
            return
        try:
            report = self.build_daily_report()
            self.send_message(report)
        except Exception as exc:
            print(
                f"[agartha][telegram] daily report error: {exc}",
                file=sys.stderr, flush=True,
            )
        # Reschedule
        self._schedule_next()

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Schedule the first daily report and send a startup notification."""
        self._stopped = False
        self._schedule_next()
        # Send a brief startup ping
        now_str = _utc_now().strftime("%Y-%m-%d %H:%M UTC")
        self.send_message(
            f"🟢 <b>Agartha Cluster iniciado</b>\n"
            f"Versión {self._version} | Modo: {self._mode}\n"
            f"Próximo reporte diario: {self._report_hour:02d}:00 UTC\n"
            f"Hora actual: {now_str}"
        )

    def stop(self) -> None:
        """Cancel any pending timer."""
        self._stopped = True
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _schedule_next(self) -> None:
        if self._stopped:
            return
        delay = _seconds_until_next(self._report_hour)
        self._timer = threading.Timer(delay, self.send_daily_report)
        self._timer.daemon = True
        self._timer.start()


# ------------------------------------------------------------------
# Factory helper
# ------------------------------------------------------------------

def try_create_notifier(
    db: "ClusterDB",
    *,
    cluster_version: str = "",
    mode: str = "live",
) -> TelegramNotifier | None:
    """Try to build a notifier from environment variables.

    Returns ``None`` (with a log line) if the credentials are not set.
    """
    import os
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        print(
            "[agartha][telegram] TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID no configurados. "
            "Notificador de Telegram desactivado.",
            file=sys.stderr, flush=True,
        )
        return None
    return TelegramNotifier(
        bot_token=token,
        chat_id=chat_id,
        db=db,
        cluster_version=cluster_version,
        mode=mode,
    )
