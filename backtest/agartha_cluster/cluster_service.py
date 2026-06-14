"""Cluster service main loop (synchronous, but cooperative).

This is a deliberately simple loop that ties scheduler, throttle, bot
runner and reconciler together. It avoids asyncio in this skeleton so
it can be exercised by tests without the event loop boilerplate; the
production live process can wrap it in an asyncio task or run it under
NSSM/systemd as-is.
"""
from __future__ import annotations

import os
import platform
import time
from dataclasses import dataclass
from typing import Optional

try:
    import psutil
except ImportError:
    psutil = None

from backtest.agartha_cluster.api_throttle import ApiThrottle
from backtest.agartha_cluster.bot_runner import BotRunner
from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.live_client import LiveClient
from backtest.agartha_cluster.models import (
    BotState,
    EventKind,
    EventLevel,
    EventSource,
    SymbolFilters,
)
from backtest.agartha_cluster.reconciler import Reconciler
from backtest.agartha_cluster.scheduler import DeployScheduler

CLUSTER_VERSION = "0.1.5"


@dataclass
class ServiceConfig:
    mode: str = "live"                          # 'live' | 'dry-run'
    tick_seconds: float = 5.0
    reconcile_every_seconds: int = 300
    capital_usdt_per_bot: float = 10.0
    initial_correlation_prefix: str = "agc"
    # Crash-recovery
    enable_recovery_boot: bool = True
    # WAL maintenance
    wal_checkpoint_every_seconds: int = 60 * 30      # 30 min, TRUNCATE mode
    # Resource logging
    resource_log_interval_seconds: int = 60


class ClusterService:
    def __init__(
        self,
        *,
        db: ClusterDB,
        client: LiveClient,
        events: EventLogger,
        throttle: ApiThrottle,
        scheduler: DeployScheduler,
        runner: BotRunner,
        reconciler: Reconciler,
        config: ServiceConfig | None = None,
    ):
        self.db = db
        self.client = client
        self.events = events
        self.throttle = throttle
        self.scheduler = scheduler
        self.runner = runner
        self.reconciler = reconciler
        self.config = config or ServiceConfig()
        self._stop = False
        # Initialise to "now" so the first reconcile and the first WAL
        # checkpoint happen after the configured interval, not on the
        # very first tick (which would surprise the operator and break
        # test assumptions about order state between place and fill).
        _now = time.time()
        self._last_reconcile = _now
        self._last_wal_checkpoint = _now
        self._last_resource_log = _now
        self._proc = psutil.Process() if psutil is not None else None
        if psutil is not None and self._proc is not None:
            try:
                psutil.cpu_percent(interval=None)
                self._proc.cpu_percent(interval=None)
            except Exception:
                pass
        self._run_id: int | None = None
        # Wire the reconciler to the runner's on_fill so missed fills
        # discovered by the periodic poll can be replayed idempotently.
        if getattr(self.reconciler, "_on_fill", None) is None:
            self.reconciler.set_on_fill(self.runner.on_fill)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._run_id = self.db.start_service_run(
            mode=self.config.mode,
            pid=os.getpid(),
            host=platform.node(),
            version=CLUSTER_VERSION,
        )
        self.events.info(
            kind=EventKind.SERVICE_START,
            source=EventSource.SERVICE,
            payload={"mode": self.config.mode, "run_id": self._run_id, "version": CLUSTER_VERSION},
        )
        if self.config.enable_recovery_boot:
            self.recovery_boot()

        if hasattr(self.client, "start_user_data_stream"):
            self.client.start_user_data_stream(self.runner.on_fill)

    # ------------------------------------------------------------------
    # Crash-recovery sweep
    # ------------------------------------------------------------------
    def recovery_boot(self) -> dict:
        """Run a one-shot recovery sweep right after :meth:`start`.

        Steps:
          1. Mark any previous ``service_runs`` whose ``stopped_at`` is
             NULL as ``crash_detected_on_restart`` and emit a critical
             event. Power-loss / SIGKILL leave such rows behind.
          2. For each order in ``pending`` / ``submitted`` / ``partially_filled``,
             call ``query_order`` to learn its true state on the exchange.
             Replay any fill that we missed (e.g. crashed mid-place_limit
             or WS disconnected). All transitions are idempotent thanks
             to the deterministic ``client_order_id``.
          3. Force a WAL checkpoint so the recovery writes are durably
             persisted before the main loop starts taking orders.
        """
        summary = {
            "previous_crashes": 0,
            "orders_queried": 0,
            "orders_filled": 0,
            "orders_cancelled": 0,
            "orders_rejected": 0,
            "fills_replayed": 0,
            "errors": 0,
        }
        self.events.info(
            kind=EventKind.SERVICE_RECOVERY_STARTED,
            source=EventSource.SERVICE,
            payload={"run_id": self._run_id},
        )

        # 1. Detect previous crashed runs.
        open_runs = self.db.list_open_service_runs(exclude_run_id=self._run_id)
        for r in open_runs:
            self.db.stop_service_run(
                int(r["run_id"]), reason="crash_detected_on_restart"
            )
            summary["previous_crashes"] += 1
            self.events.critical(
                kind=EventKind.SERVICE_PREVIOUS_CRASH_DETECTED,
                source=EventSource.SERVICE,
                payload={
                    "prev_run_id": int(r["run_id"]),
                    "prev_pid": r["pid"],
                    "started_at": r["started_at"],
                    "mode": r["mode"],
                    "host": r["host"],
                },
            )

        # 2. Re-query open orders and replay missed fills via the reconciler
        #    (same code path that runs every reconcile tick).
        try:
            poll = self.reconciler.poll_open_orders_for_fills()
            summary["orders_queried"] = poll.get("queried", 0)
            summary["orders_filled"] = poll.get("filled", 0)
            summary["orders_cancelled"] = poll.get("cancelled", 0)
            summary["orders_rejected"] = poll.get("rejected", 0)
            summary["fills_replayed"] = poll.get("replayed", 0)
            summary["errors"] = poll.get("errors", 0)
        except NotImplementedError:
            # Real client not wired yet; the rest of recovery still applies.
            pass

        # 3. Durably persist recovery writes via a WAL checkpoint.
        try:
            self.db.wal_checkpoint(mode="TRUNCATE")
        except Exception as e:
            import sys
            print(
                f"[agartha][Error] Recovery WAL checkpoint failed: {e}",
                file=sys.stderr,
                flush=True,
            )

        self.events.info(
            kind=EventKind.SERVICE_RECOVERY_COMPLETED,
            source=EventSource.SERVICE,
            payload=summary,
        )
        return summary

    def stop(self, reason: str = "graceful_shutdown") -> None:
        self._stop = True
        if hasattr(self.client, "stop_user_data_stream"):
            try:
                self.client.stop_user_data_stream()
            except Exception as e:
                import sys
                print(
                    f"[agartha][Error] stop_user_data_stream failed: {e}",
                    file=sys.stderr,
                    flush=True,
                )
        if self._run_id is not None:
            self.db.stop_service_run(self._run_id, reason=reason)
        self.events.info(
            kind=EventKind.SERVICE_STOP,
            source=EventSource.SERVICE,
            payload={"reason": reason},
        )

    def request_stop(self) -> None:
        self._stop = True

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    def run_forever(self) -> None:
        if self._run_id is None:
            self.start()
        try:
            while not self._stop:
                self.tick_once()
                time.sleep(self.config.tick_seconds)
        finally:
            if not self._stop:
                self.stop()

    def tick_once(self) -> None:
        # 1. Try to deploy the next due bot (one per tick max).
        row = self.scheduler.pick_next()
        if row is not None:
            self._deploy_from_queue(row)

        # 2. Walk through in-flight bots and advance their state.
        for bot in self.db.list_bots(state=BotState.IN_POSITION):
            filters = self.db.get_symbol_filters(bot.symbol) or SymbolFilters(symbol=bot.symbol)
            self.runner.place_exit(bot, filters)

        # 3. Periodic reconciliation.
        if (time.time() - self._last_reconcile) >= self.config.reconcile_every_seconds:
            try:
                self.reconciler.run_once()
            except NotImplementedError:
                # Real client not wired yet; reconciler is a no-op in this case.
                pass
            self._last_reconcile = time.time()

        # 4. Periodic WAL checkpoint (bounds WAL file size, speeds future
        #    recoveries). TRUNCATE shrinks the WAL back to 0 bytes.
        if (
            self.config.wal_checkpoint_every_seconds > 0
            and (time.time() - self._last_wal_checkpoint)
            >= self.config.wal_checkpoint_every_seconds
        ):
            try:
                self.db.wal_checkpoint(mode="TRUNCATE")
            except Exception:
                pass
            self._last_wal_checkpoint = time.time()

        # 5. Periodic resource monitoring logging.
        if (
            self.config.resource_log_interval_seconds >= 0
            and (time.time() - self._last_resource_log)
            >= self.config.resource_log_interval_seconds
        ):
            self._log_resources()
            self._last_resource_log = time.time()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _deploy_from_queue(self, row) -> None:
        symbol = row["symbol"]
        params = self.db.get_symbol_params(symbol)
        if params is None:
            self.db.mark_queue_status(
                row["queue_id"],
                "failed",
                reason="no_params; run optimizer first",
            )
            self.events.error(
                kind=EventKind.OPTIMIZATION_FAILED,
                source=EventSource.SCHEDULER,
                symbol=symbol,
                payload={"queue_id": row["queue_id"], "reason": "missing_symbol_params"},
            )
            return

        filters = self.db.get_symbol_filters(symbol)
        if filters is None:
            try:
                raw = self.client.get_filters(symbol)
                filters = SymbolFilters(
                    symbol=symbol,
                    tick_size=float(raw.get("tick_size", 1e-8)),
                    step_size=float(raw.get("step_size", 1e-8)),
                    min_notional=float(raw.get("min_notional", 0.1)),
                    bid_multiplier_up=float(raw.get("bid_multiplier_up", 5.0)),
                    bid_multiplier_down=float(raw.get("bid_multiplier_down", 0.2)),
                    ask_multiplier_up=float(raw.get("ask_multiplier_up", 5.0)),
                    ask_multiplier_down=float(raw.get("ask_multiplier_down", 0.2)),
                    refreshed_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                )
                self.db.upsert_symbol_filters(filters, raw=raw)
            except NotImplementedError:
                self.db.mark_queue_status(
                    row["queue_id"], "failed", reason="filters_unavailable_live_client_not_wired"
                )
                self.events.error(
                    kind=EventKind.NEEDS_MANUAL_ACTION,
                    source=EventSource.SCHEDULER,
                    symbol=symbol,
                    payload={"reason": "BinanceAlphaClient.get_filters_not_wired"},
                )
                return

        correlation_id = f"{self.config.initial_correlation_prefix}-{row['queue_id']}-{int(time.time())}"
        bot_id = self.db.create_bot(
            symbol=symbol,
            capital_usdt=self.config.capital_usdt_per_bot,
            params_snapshot=params.as_runtime_dict(),
            correlation_id=correlation_id,
            state=BotState.QUEUED,
        )
        self.db.mark_queue_status(
            row["queue_id"], "deployed", bot_id=bot_id, actual_deploy_ts=int(time.time() * 1000)
        )
        self.events.info(
            kind=EventKind.BOT_DEPLOYED,
            source=EventSource.SCHEDULER,
            bot_id=bot_id,
            symbol=symbol,
            correlation_id=correlation_id,
            payload={"queue_id": row["queue_id"], "capital_usdt": self.config.capital_usdt_per_bot},
        )

        bot = self.db.get_bot(bot_id)
        if bot is not None:
            self.runner.place_entry(bot, filters)

    def _log_resources(self) -> None:
        if psutil is None:
            return
        try:
            proc_cpu = 0.0
            proc_ram = 0.0
            host_cpu = 0.0
            host_ram = 0.0

            try:
                host_cpu = float(psutil.cpu_percent(interval=None))
                vm = psutil.virtual_memory()
                host_ram = float(vm.percent)
            except Exception:
                pass

            if self._proc is not None:
                try:
                    proc_cpu = float(self._proc.cpu_percent(interval=None))
                    proc_ram = float(self._proc.memory_info().rss / (1024.0 * 1024.0))
                except Exception:
                    pass

            disk_used = 0.0
            disk_free = 0.0
            disk_pct = 0.0
            try:
                disk = psutil.disk_usage('.')
                disk_used = float(disk.used / (1024.0 * 1024.0 * 1024.0))
                disk_free = float(disk.free / (1024.0 * 1024.0 * 1024.0))
                disk_pct = float(disk.percent)
            except Exception:
                pass

            self.db.insert_resource_metric(
                ts_ms=int(time.time() * 1000),
                proc_cpu_pct=proc_cpu,
                proc_ram_mb=proc_ram,
                host_cpu_pct=host_cpu,
                host_ram_pct=host_ram,
                disk_used_gb=disk_used,
                disk_free_gb=disk_free,
                disk_pct=disk_pct,
            )
        except Exception:
            pass
