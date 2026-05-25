"""CLI command implementations for the Agartha cluster.

The thin entrypoint lives in ``scripts/agartha_cluster_cli.py``. Logic
is here so it can be unit tested without spawning subprocesses.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from backtest.agartha_cluster import credentials as creds_mod
from backtest.agartha_cluster.api_throttle import ApiThrottle, ThrottleConfig
from backtest.agartha_cluster.bot_runner import BotRunner, RunnerConfig
from backtest.agartha_cluster.cluster_db import ClusterDB
from backtest.agartha_cluster.cluster_service import ClusterService, ServiceConfig
from backtest.agartha_cluster.event_logger import EventLogger
from backtest.agartha_cluster.live_client import (
    BinanceAlphaClient,
    LiveClient,
    StubLiveClient,
)
from backtest.agartha_cluster.models import (
    BotState,
    EventKind,
    EventLevel,
    EventSource,
    SymbolParams,
)
from backtest.agartha_cluster.reconciler import Reconciler
from backtest.agartha_cluster.scheduler import DeployScheduler, SchedulerConfig

DEFAULT_DB = "cluster.db"
DEFAULT_LOG_DIR = "logs/agartha_cluster"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_db(path: str) -> ClusterDB:
    return ClusterDB(path).__enter__()


def _build_client(dry_run: bool, db: ClusterDB) -> LiveClient:
    if dry_run:
        return StubLiveClient(seed=42, initial_quote_balance=10_000.0)
    creds = creds_mod.ensure_credentials(db, interactive=True)
    return BinanceAlphaClient(api_key=creds.api_key, api_secret=creds.api_secret)


def _build_service(
    *,
    db: ClusterDB,
    client: LiveClient,
    capital_usdt: float,
    slot_seconds: int,
    mode: str,
    log_dir: str,
) -> ClusterService:
    events = EventLogger(db, jsonl_dir=log_dir, echo_stdout=True)
    throttle = ApiThrottle(db, ThrottleConfig())
    scheduler = DeployScheduler(
        db,
        throttle,
        events,
        SchedulerConfig(slot_seconds=slot_seconds),
    )
    runner = BotRunner(db=db, client=client, throttle=throttle, events=events, config=RunnerConfig())
    reconciler = Reconciler(db, client, events)
    service = ClusterService(
        db=db,
        client=client,
        events=events,
        throttle=throttle,
        scheduler=scheduler,
        runner=runner,
        reconciler=reconciler,
        config=ServiceConfig(
            mode=mode,
            capital_usdt_per_bot=capital_usdt,
        ),
    )
    return service


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------


def cmd_init_db(args: argparse.Namespace) -> int:
    db = ClusterDB(args.db)
    db.init_schema()
    version = db.schema_version()
    print(f"cluster.db initialised at {Path(args.db).resolve()} (schema v{version})")
    db.close()
    return 0


def cmd_load_universe(args: argparse.Namespace) -> int:
    src = Path(args.from_json) if args.from_json else None
    if src is None or not src.exists():
        print(
            "Provide --from-json <path> with the Alpha token list "
            "(produced by scripts/download_and_prepare_alpha.py or similar).",
            file=sys.stderr,
        )
        return 2
    payload = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        print("Expected a JSON array of token records.", file=sys.stderr)
        return 2
    db = ClusterDB(args.db)
    db.init_schema()
    rows = [
        {
            "symbol": r["symbol"],
            "alpha_id": r.get("alpha_id"),
            "quote_asset": r.get("quote_asset"),
            "listing_ts": r.get("listing_ts"),
            "last_seen_ts": int(time.time() * 1000),
            "status": r.get("status", "eligible"),
            "holders": r.get("holders"),
            "liquidity_usd": r.get("liquidity_usd"),
            "metadata": r.get("metadata"),
        }
        for r in payload
    ]
    n = db.upsert_universe(rows)
    db.close()
    print(f"Universe upsert: {n} rows.")
    return 0


def cmd_set_params(args: argparse.Namespace) -> int:
    db = ClusterDB(args.db)
    db.init_schema()
    params = SymbolParams(
        symbol=args.symbol,
        trailing_stop_pct=args.trailing,
        activation_profit_pct=args.activation,
        breakeven_lock_pct=args.breakeven,
        entry_limit_offset_pct=args.entry_offset,
        partial_tp_pct=args.partial_tp,
        partial_tp_size_pct=args.partial_size,
        max_holding_bars=args.max_bars,
        optimized_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    db.upsert_symbol_params(params)
    db.close()
    print(f"Stored params for {args.symbol}.")
    return 0


def cmd_schedule_batch(args: argparse.Namespace) -> int:
    db = ClusterDB(args.db)
    db.init_schema()
    events = EventLogger(db, jsonl_dir=args.log_dir, echo_stdout=False)
    throttle = ApiThrottle(db, ThrottleConfig())
    scheduler = DeployScheduler(
        db, throttle, events, SchedulerConfig(slot_seconds=args.slot_seconds)
    )

    eligible = [r["symbol"] for r in db.list_universe(status=args.status)]
    if args.limit:
        eligible = eligible[: args.limit]
    if not eligible:
        print("No symbols match the criteria.", file=sys.stderr)
        db.close()
        return 1
    ids = scheduler.enqueue_symbols(eligible, priority=args.priority)
    db.close()
    print(f"Enqueued {len(ids)} symbols (slot={args.slot_seconds}s).")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db = ClusterDB(args.db)
    bots = db.list_bots(limit=args.limit)
    print(f"{'BOT':>5}  {'SYMBOL':<14}  {'STATE':<22}  {'PNL':>10}  CORR")
    print("-" * 80)
    for b in bots:
        pnl = f"{b.realized_pnl_usdt:.4f}" if b.realized_pnl_usdt is not None else "-"
        print(f"{b.bot_id:>5}  {b.symbol:<14}  {b.state.value:<22}  {pnl:>10}  {b.correlation_id}")
    db.close()
    return 0


def cmd_creds(args: argparse.Namespace) -> int:
    db = ClusterDB(args.db)
    db.init_schema()
    if args.action == "set":
        creds_mod.prompt_and_store(db, profile=args.profile)
    elif args.action == "check":
        c = creds_mod.load(db, profile=args.profile)
        if c is None:
            print(f"No credentials stored for profile '{args.profile}'.")
            db.close()
            return 1
        print(f"Credentials present for profile '{args.profile}' (service={c.service_name}).")
    elif args.action == "rotate":
        creds_mod.delete(db, profile=args.profile)
        creds_mod.prompt_and_store(db, profile=args.profile)
    else:
        print(f"unknown action: {args.action}", file=sys.stderr)
        db.close()
        return 2
    db.close()
    return 0


def cmd_live_up(args: argparse.Namespace) -> int:
    db = ClusterDB(args.db)
    db.init_schema()
    client = _build_client(args.dry_run, db)
    service = _build_service(
        db=db,
        client=client,
        capital_usdt=args.capital_usdt,
        slot_seconds=args.slot_seconds,
        mode="dry-run" if args.dry_run else "live",
        log_dir=args.log_dir,
    )
    service.start()
    try:
        if args.ticks:
            for _ in range(args.ticks):
                service.tick_once()
        else:
            service.run_forever()
    except KeyboardInterrupt:
        service.stop("keyboard_interrupt")
    finally:
        service.stop()
        db.close()
    return 0


def cmd_supervisor_close(args: argparse.Namespace) -> int:
    db = ClusterDB(args.db)
    client = _build_client(args.dry_run, db)
    events = EventLogger(db, jsonl_dir=args.log_dir, echo_stdout=True)
    throttle = ApiThrottle(db, ThrottleConfig())
    runner = BotRunner(db=db, client=client, throttle=throttle, events=events)
    bot = db.get_bot(args.bot_id)
    if bot is None:
        print(f"bot_id {args.bot_id} not found", file=sys.stderr)
        db.close()
        return 1
    runner.manual_close(bot, reason=args.reason)
    db.close()
    return 0


def cmd_supervisor_list_stale(args: argparse.Namespace) -> int:
    db = ClusterDB(args.db)
    stale = db.list_bots(state=BotState.STALE_EXIT)
    print(f"{'BOT':>5}  {'SYMBOL':<14}  CORR")
    for b in stale:
        print(f"{b.bot_id:>5}  {b.symbol:<14}  {b.correlation_id}")
    db.close()
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    db = ClusterDB(args.db)
    bots = db.list_bots(symbol=args.symbol, limit=args.limit)
    if not bots:
        print(f"No bots for symbol {args.symbol}.")
        db.close()
        return 1
    for b in bots:
        print(f"--- bot {b.bot_id} [{b.state.value}] ---")
        print(f"  capital_usdt = {b.capital_usdt}")
        print(f"  params       = {b.params_snapshot_json}")
        print(f"  entry        = {b.entry_qty}@{b.entry_price}")
        print(f"  peak/floor   = {b.peak_price}/{b.trail_floor}")
        print(f"  exit         = {b.exit_qty}@{b.exit_price}")
        print(f"  pnl_usdt     = {b.realized_pnl_usdt}")
        print(f"  notes        = {b.notes}")
        events = db.query_events(bot_id=b.bot_id, limit=20)
        print(f"  last events  ({len(events)}):")
        for ev in events:
            print(f"    {ev['ts_ms']}  {ev['kind']:24s}  {ev['level']:8s}  {ev['source']}")
    db.close()
    return 0


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agartha_cluster", description="Agartha cluster operations")
    p.add_argument("--db", default=DEFAULT_DB, help="Path to cluster.db (default: cluster.db)")
    p.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help="JSONL event log directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("init-db", help="Apply DB migrations.")
    sp.set_defaults(func=cmd_init_db)

    sp = sub.add_parser("load-universe", help="Upsert symbols from a JSON file.")
    sp.add_argument("--from-json", required=True, help="Path to JSON array of token records.")
    sp.set_defaults(func=cmd_load_universe)

    sp = sub.add_parser("set-params", help="Manually store best params for a symbol.")
    sp.add_argument("symbol")
    sp.add_argument("--trailing", type=float, required=True)
    sp.add_argument("--activation", type=float, default=0.0)
    sp.add_argument("--breakeven", type=float, default=0.0)
    sp.add_argument("--entry-offset", type=float, default=0.0)
    sp.add_argument("--partial-tp", type=float, default=0.0)
    sp.add_argument("--partial-size", type=float, default=0.0)
    sp.add_argument("--max-bars", type=int, default=0)
    sp.set_defaults(func=cmd_set_params)

    sp = sub.add_parser("schedule-batch", help="Enqueue eligible symbols spaced by slot.")
    sp.add_argument("--status", default="eligible")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--slot-seconds", type=int, default=600)
    sp.add_argument("--priority", type=int, default=100)
    sp.set_defaults(func=cmd_schedule_batch)

    sp = sub.add_parser("status", help="Show recent bots.")
    sp.add_argument("--limit", type=int, default=50)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("report", help="Report all deployments of one symbol.")
    sp.add_argument("symbol")
    sp.add_argument("--limit", type=int, default=10)
    sp.set_defaults(func=cmd_report)

    sp = sub.add_parser("creds", help="Manage Binance Alpha credentials.")
    sp.add_argument("action", choices=["set", "check", "rotate"])
    sp.add_argument("--profile", default="default")
    sp.set_defaults(func=cmd_creds)

    sp = sub.add_parser("live-up", help="Start the cluster service.")
    sp.add_argument("--dry-run", action="store_true", help="Use StubLiveClient (no network).")
    sp.add_argument("--capital-usdt", type=float, default=10.0)
    sp.add_argument("--slot-seconds", type=int, default=600)
    sp.add_argument("--ticks", type=int, default=0, help=">0 runs that many ticks and exits.")
    sp.set_defaults(func=cmd_live_up)

    sp = sub.add_parser("supervisor", help="Supervisor commands.")
    ssub = sp.add_subparsers(dest="action", required=True)

    spc = ssub.add_parser("close", help="Force a manual close of a bot.")
    spc.add_argument("bot_id", type=int)
    spc.add_argument("--reason", required=True)
    spc.add_argument("--dry-run", action="store_true")
    spc.set_defaults(func=cmd_supervisor_close)

    spl = ssub.add_parser("list-stale", help="List bots in STALE_EXIT.")
    spl.set_defaults(func=cmd_supervisor_list_stale)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
