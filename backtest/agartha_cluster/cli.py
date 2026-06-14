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
    if hasattr(client, "set_throttle"):
        client.set_throttle(throttle)
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
    if not args.from_json and not args.from_binance:
        print(
            "Provide --from-json <path> (offline) or --from-binance (live REST).",
            file=sys.stderr,
        )
        return 2
    if args.from_binance:
        payload = _fetch_alpha_token_list_from_binance(
            include_offline=args.include_offline,
            include_offsell=args.include_offsell,
        )
        if args.export_json:
            Path(args.export_json).parent.mkdir(parents=True, exist_ok=True)
            Path(args.export_json).write_text(
                json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            print(f"Token list exported to {args.export_json}.")
    else:
        src = Path(args.from_json)
        if not src.exists():
            print(f"--from-json file not found: {src}", file=sys.stderr)
            return 2
        payload = json.loads(src.read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            print("Expected a JSON array of token records.", file=sys.stderr)
            return 2

    if args.limit:
        payload = payload[: int(args.limit)]

    db = ClusterDB(args.db)
    db.init_schema()
    rows = [_normalise_universe_row(r) for r in payload]
    rows = [r for r in rows if r is not None]
    n = db.upsert_universe(rows)
    db.close()
    print(f"Universe upsert: {n} rows.")
    return 0


def _fetch_alpha_token_list_from_binance(
    *,
    include_offline: bool,
    include_offsell: bool,
) -> list[dict]:
    """REST call to Alpha token list. Filters offline/offsell by default."""
    from binance_hist_downloader import BinanceDownloader

    dl = BinanceDownloader()
    tokens = dl.get_alpha_token_list()
    keep: list[dict] = []
    for t in tokens:
        if not include_offline and bool(t.get("offline", False)):
            continue
        if not include_offsell and bool(t.get("offsell", False)):
            continue
        keep.append(t)
    print(
        f"[load-universe] Binance Alpha token list: total={len(tokens)} "
        f"kept_after_filter={len(keep)} "
        f"(include_offline={include_offline}, include_offsell={include_offsell})"
    )
    return keep


def _normalise_universe_row(token: dict) -> Optional[dict]:
    """Map a token dict (from Binance or local JSON) to the universe row shape."""
    sym = token.get("symbol") or token.get("baseAsset")
    if not sym:
        return None
    sym = str(sym).upper()
    alpha_id = token.get("alpha_id") or token.get("alphaId")
    liquidity = token.get("liquidity_usd")
    if liquidity is None:
        liquidity = token.get("liquidity")
    try:
        liquidity = float(liquidity) if liquidity is not None else None
    except (TypeError, ValueError):
        liquidity = None
    holders = token.get("holders")
    try:
        holders = int(holders) if holders is not None else None
    except (TypeError, ValueError):
        holders = None
    metadata = token.get("metadata")
    if metadata is None:
        metadata = {
            k: v
            for k, v in token.items()
            if k
            not in {
                "symbol",
                "alphaId",
                "alpha_id",
                "liquidity",
                "liquidity_usd",
                "holders",
                "status",
                "quote_asset",
                "listing_ts",
                "metadata",
            }
        }
    return {
        "symbol": sym,
        "alpha_id": alpha_id,
        "quote_asset": token.get("quote_asset"),
        "listing_ts": token.get("listing_ts") or token.get("listingTime"),
        "last_seen_ts": int(time.time() * 1000),
        "status": token.get("status", "eligible"),
        "holders": holders,
        "liquidity_usd": liquidity,
        "metadata": metadata,
    }


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


def cmd_import_params(args: argparse.Namespace) -> int:
    """Promote Optuna best_trial(s) into ``symbol_params``.

    Three modes:
      1. Single study: ``--symbol SYM --study NAME``.
      2. Batch JSON: ``--batch-json path`` with
         ``[{"symbol":"FOOUSDT","study":"agartha_foo_15m"}, ...]``.
      3. ``--storage-path`` overrides the convention-resolved Optuna DB path.
    """
    items: list[tuple[str, str]] = []
    if args.batch_json:
        rows = json.loads(Path(args.batch_json).read_text(encoding="utf-8"))
        for r in rows:
            sym = r.get("symbol")
            study = r.get("study")
            if not sym or not study:
                print(f"  SKIP malformed row: {r}", file=sys.stderr)
                continue
            items.append((str(sym).upper(), str(study)))
    elif args.symbol and args.study:
        items.append((args.symbol.upper(), args.study))
    else:
        print(
            "Provide either --batch-json <file> or both --symbol and --study.",
            file=sys.stderr,
        )
        return 2

    db = ClusterDB(args.db)
    db.init_schema()
    ok = 0
    fail = 0
    for symbol, study in items:
        try:
            best = _load_best_params(
                study=study,
                root=args.root,
                storage_path=args.storage_path,
            )
            db.upsert_universe([
                {
                    "symbol": symbol,
                    "status": "eligible",
                    "last_seen_ts": int(time.time() * 1000),
                }
            ])
            sp = SymbolParams(
                symbol=symbol,
                trailing_stop_pct=float(best["trailing_stop_pct"]),
                activation_profit_pct=float(best.get("activation_profit_pct", 0.0) or 0.0),
                breakeven_lock_pct=float(best.get("breakeven_lock_pct", 0.0) or 0.0),
                entry_limit_offset_pct=float(best.get("entry_limit_offset_pct", 0.0) or 0.0),
                study_trial_id=(
                    str(best.get("trial_number"))
                    if best.get("trial_number") is not None
                    else None
                ),
                study_equity_pct=(
                    float(best.get("value")) if best.get("value") is not None else None
                ),
                optimized_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                optuna_db_path=best.get("source"),
            )
            db.upsert_symbol_params(sp, raw_params=best)
            ok += 1
            print(
                f"  OK   {symbol:<14} <- {study}  "
                f"trail={sp.trailing_stop_pct} act={sp.activation_profit_pct} "
                f"be={sp.breakeven_lock_pct} off={sp.entry_limit_offset_pct} "
                f"value={best.get('value')}"
            )
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL {symbol:<14} <- {study}  ({e})", file=sys.stderr)
            fail += 1

    db.close()
    print(f"Imported params: ok={ok} fail={fail} total={len(items)}")
    return 0 if fail == 0 else 1


def _load_best_params(
    *,
    study: str,
    root: str,
    storage_path: Optional[str],
) -> dict:
    """Return best_params dict.

    Resolution order:
      1. ``storage_path`` if given and exists -> Optuna load_study.
      2. Convention path ``<root>/entregables/studies/<study>/optuna.db`` -> Optuna.
      3. Sibling ``trial_to_run.json`` -> read ``best_params`` directly (no optuna dep).
    """
    candidate_paths: list[Path] = []
    if storage_path:
        candidate_paths.append(Path(storage_path))
    candidate_paths.append(Path(root) / "entregables" / "studies" / study / "optuna.db")

    for db_path in candidate_paths:
        if db_path.exists():
            try:
                import optuna  # type: ignore[import-untyped]
            except ImportError:
                break
            storage_url = f"sqlite:///{db_path.resolve().as_posix()}"
            st = optuna.load_study(study_name=study, storage=storage_url)
            return {
                **st.best_params,
                "trial_number": st.best_trial.number,
                "value": float(st.best_value),
                "source": str(db_path),
            }

    for db_path in candidate_paths:
        json_path = db_path.parent / "trial_to_run.json"
        if json_path.exists():
            data = json.loads(json_path.read_text(encoding="utf-8"))
            return {
                **(data.get("best_params") or {}),
                "trial_number": data.get("best_trial_number"),
                "value": data.get("best_value"),
                "source": str(json_path),
            }

    raise FileNotFoundError(
        f"No optuna.db or trial_to_run.json found for study '{study}' "
        f"(checked: {[str(p) for p in candidate_paths]})"
    )


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
    if hasattr(client, "set_throttle"):
        client.set_throttle(throttle)
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


def cmd_report_resources(args: argparse.Namespace) -> int:
    db = _open_db(args.db)
    since_ms = int((time.time() - (args.days * 86400)) * 1000)
    metrics = db.get_resource_metrics(since_ms=since_ms)
    db.close()

    if not metrics:
        print(f"No hay métricas registradas en los últimos {args.days} días.")
        return 0

    n = len(metrics)
    proc_cpus = [m["proc_cpu_pct"] for m in metrics]
    proc_rams = [m["proc_ram_mb"] for m in metrics]
    host_cpus = [m["host_cpu_pct"] for m in metrics]
    host_rams = [m["host_ram_pct"] for m in metrics]
    disk_useds = [m["disk_used_gb"] for m in metrics]
    disk_frees = [m["disk_free_gb"] for m in metrics]
    disk_pcts = [m["disk_pct"] for m in metrics]

    def avg(lst): return sum(lst) / len(lst)
    def p95(lst):
        s = sorted(lst)
        idx = int(len(s) * 0.95)
        return s[min(idx, len(s) - 1)]
    def peak(lst): return max(lst)

    avg_p_cpu, p95_p_cpu, peak_p_cpu = avg(proc_cpus), p95(proc_cpus), peak(proc_cpus)
    avg_p_ram, p95_p_ram, peak_p_ram = avg(proc_rams), p95(proc_rams), peak(proc_rams)
    avg_h_cpu, p95_h_cpu, peak_h_cpu = avg(host_cpus), p95(host_cpus), peak(host_cpus)
    avg_h_ram, p95_h_ram, peak_h_ram = avg(host_rams), p95(host_rams), peak(host_rams)
    max_disk_used, min_disk_free, max_disk_pct = peak(disk_useds), min(disk_frees), peak(disk_pcts)

    print(f"============================================================")
    print(f"REPORTE DE CONSUMO DE RECURSOS ({args.days} días, {n} muestras)")
    print(f"============================================================")
    print(f"Métrica            | Promedio    | Percentil 95 | Pico / Max  ")
    print(f"------------------------------------------------------------")
    print(f"CPU Proceso (%)    | {avg_p_cpu:11.2f} | {p95_p_cpu:12.2f} | {peak_p_cpu:11.2f}")
    print(f"CPU Host (%)       | {avg_h_cpu:11.2f} | {p95_h_cpu:12.2f} | {peak_h_cpu:11.2f}")
    print(f"RAM Proceso (MB)   | {avg_p_ram:11.2f} | {p95_p_ram:12.2f} | {peak_p_ram:11.2f}")
    print(f"RAM Host (%)       | {avg_h_ram:11.2f} | {p95_h_ram:12.2f} | {peak_h_ram:11.2f}")
    print(f"------------------------------------------------------------")
    print(f"Almacenamiento (Carpeta actual / Base de datos):")
    print(f"  - Máximo Uso de Disco: {max_disk_used:.2f} GB ({max_disk_pct:.2f}%)")
    print(f"  - Mínimo Disco Libre:  {min_disk_free:.2f} GB")
    print(f"============================================================")

    # Cloud sizing recommendation logic
    # Cores
    if peak_h_cpu < 30.0:
        rec_cores = 1
    elif peak_h_cpu < 70.0:
        rec_cores = 2
    else:
        rec_cores = 4

    # Memory
    needed_ram_gb = (p95_p_ram / 1024.0) + 1.0
    if needed_ram_gb <= 1.0:
        rec_ram_gb = 1
    elif needed_ram_gb <= 2.0:
        rec_ram_gb = 2
    elif needed_ram_gb <= 4.0:
        rec_ram_gb = 4
    else:
        rec_ram_gb = 8

    # Disk
    needed_disk_gb = max_disk_used + 25.0
    if needed_disk_gb <= 20.0:
        rec_disk_gb = 20
    elif needed_disk_gb <= 40.0:
        rec_disk_gb = 40
    elif needed_disk_gb <= 80.0:
        rec_disk_gb = 80
    else:
        rec_disk_gb = 160

    print(f"\nDISEÑO Y RECOMENDACIÓN DE SERVICIO CLOUD (VPS / DEDICADO):")
    print(f"------------------------------------------------------------")
    print(f"1. Recomendación para ejecución remota en la nube (Solo Live trading):")
    print(f"   - vCPUs recomendados:   {rec_cores} Core(s)")
    print(f"   - RAM recomendada:     {rec_ram_gb} GB")
    print(f"   - Almacenamiento SSD:  {rec_disk_gb} GB")
    print(f"   * Perfiles de ejemplo sugeridos:")
    if rec_ram_gb <= 1:
        print(f"     - AWS: EC2 t3.micro (1 vCPU, 1 GB RAM)")
        print(f"     - DigitalOcean: Starter Droplet (1 vCPU, 1 GB RAM)")
    elif rec_ram_gb <= 2:
        print(f"     - AWS: EC2 t3.small (2 vCPU, 2 GB RAM)")
        print(f"     - DigitalOcean: Basic Droplet (1 vCPU, 2 GB RAM / 2 vCPU, 2 GB RAM)")
    else:
        print(f"     - AWS: EC2 t3.medium (2 vCPU, 4 GB RAM) o t3.large")
        print(f"     - DigitalOcean: General Purpose / Basic Droplet (4 GB o 8 GB RAM)")

    print(f"\n2. Recomendación para ejecución con Optimización pesada local (Optuna):")
    print(f"   - Si decide correr Optuna/Ray y backtests pesados en el mismo servidor en vivo:")
    print(f"     - Se aconseja un servidor de al menos: 4 a 8 vCPUs dedicados, 16 GB de RAM, y 100 GB+ SSD.")
    print(f"     - Esto se debe a que la optimización requiere mucha CPU y memoria paralela, además de la base")
    print(f"       de datos klines.db que actualmente pesa ~70.8 GB.")
    print(f"   - Tesis Recomendada: Seguir corriendo Optuna de forma pesada y local una vez a la semana,")
    print(f"     y únicamente cargar las configuraciones al cluster en la nube (que puede ser un VPS pequeño).")
    print(f"============================================================")

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

    sp = sub.add_parser(
        "load-universe",
        help="Upsert symbols from a JSON file or live Binance Alpha REST call.",
    )
    grp = sp.add_mutually_exclusive_group(required=True)
    grp.add_argument("--from-json", help="Path to JSON array of token records.")
    grp.add_argument(
        "--from-binance",
        action="store_true",
        help="Call Binance Alpha token list endpoint directly.",
    )
    sp.add_argument(
        "--include-offline",
        action="store_true",
        help="Keep tokens flagged offline (default: drop them).",
    )
    sp.add_argument(
        "--include-offsell",
        action="store_true",
        help="Keep tokens flagged offsell (default: drop them).",
    )
    sp.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Limit the number of rows upserted (0 = no limit).",
    )
    sp.add_argument(
        "--export-json",
        default=None,
        help="When using --from-binance, also save the raw token list to this path.",
    )
    sp.set_defaults(func=cmd_load_universe)

    sp = sub.add_parser(
        "import-params",
        help="Promote Optuna best_trial into symbol_params (single or batch).",
    )
    sp.add_argument("--symbol", help="Single import: target symbol.")
    sp.add_argument("--study", help="Single import: Optuna study name.")
    sp.add_argument(
        "--batch-json",
        help='Batch import: JSON list of {"symbol": "...", "study": "..."}.',
    )
    sp.add_argument(
        "--root",
        default="reports",
        help="Reports root used to resolve <root>/entregables/studies/<study>/optuna.db.",
    )
    sp.add_argument(
        "--storage-path",
        default=None,
        help="Override path to the Optuna SQLite file (skips convention resolution).",
    )
    sp.set_defaults(func=cmd_import_params)

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

    sp = sub.add_parser(
        "report-resources",
        help="Analiza registros de consumo de recursos y recomienda VPS.",
    )
    sp.add_argument(
        "--days",
        type=int,
        default=30,
        help="Número de días de historial a analizar (default: 30).",
    )
    sp.set_defaults(func=cmd_report_resources)

    return p



def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
