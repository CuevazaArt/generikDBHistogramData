"""High-level run orchestration (engine + storage)."""
import os
from typing import Dict, Optional, Type

from backtest.engine import BacktestResult, EngineConfig, run_backtest
from backtest.report_paths import run_report_dir
from backtest.storage import create_run, finish_run, persist_run_events, persist_run_metrics
from backtest.storage_paths import StoragePaths
from backtest.strategy_base import StrategyBase
from db import init_db


def _parquet_events_enabled() -> bool:
    return os.getenv("BACKTEST_EVENTS_PARQUET", "0").strip().lower() in {"1", "true", "yes", "on"}


def _resume_run_id_from_env() -> Optional[int]:
    raw = os.getenv("BACKTEST_RESUME_RUN_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _resolve_checkpoints_dir(run_id: int) -> str:
    """Filesystem path for a run's checkpoint directory.

    Honours ``BACKTEST_DATA_ROOT`` so test harnesses and production layouts
    can both share the same logic.
    """
    paths = StoragePaths(os.getenv("BACKTEST_DATA_ROOT", "data"))
    return paths.checkpoints_dir(int(run_id))


def _emit_resume_audit(run_id: int, checkpoint_path: str) -> None:
    """Best-effort `ops.audit_log` insert for a resume event.

    Silently skipped when the active backend is SQLite or when the PG
    storage facade is unavailable; the resume itself still proceeds because
    the audit row is informational, not load-bearing.
    """
    try:
        from backtest.storage_facade import get_storage
    except ImportError:
        return
    try:
        backend = get_storage()
    except Exception:
        return
    if getattr(backend, "kind", "") != "pg":
        return
    try:
        from backtest import storage_pg  # type: ignore

        with storage_pg.transaction(backend.dsn) as conn:  # type: ignore[attr-defined]
            with conn.cursor() as cur:
                import json as _json

                cur.execute(
                    """
                    INSERT INTO ops.audit_log (run_id, event_type, payload)
                    VALUES (%s, %s, %s::jsonb)
                    """,
                    (int(run_id), "resume", _json.dumps({"checkpoint_path": str(checkpoint_path)})),
                )
    except Exception:
        return


def execute_and_persist_resumable(
    config: EngineConfig,
    strategy_cls: Type[StrategyBase],
    strategy_params: Optional[Dict] = None,
    trial_id: Optional[int] = None,
    initial_state: Optional[Dict] = None,
    events_batch_size: int = 5000,
) -> BacktestResult:
    """Resume-aware variant of :func:`execute_and_persist`.

    Reads ``BACKTEST_RESUME_RUN_ID`` from the environment (set by the
    ``--resume <run_id>`` CLI flag) and, if present, locates the latest
    checkpoint for that run and patches ``config.resume_from_checkpoint``
    plus ``config.checkpoints_dir`` before calling
    :func:`execute_and_persist`. If the env var is unset, missing, or no
    checkpoint exists on disk, this is a transparent passthrough.

    A short ``[resume]`` line is printed to stdout when a checkpoint is
    actually being honoured so operators can confirm the dispatch path. A
    best-effort row is written to ``ops.audit_log`` when the active backend
    is PostgreSQL.
    """
    from backtest.checkpoint import latest_checkpoint_path

    requested_run_id = _resume_run_id_from_env()
    if requested_run_id is None:
        return execute_and_persist(
            config=config,
            strategy_cls=strategy_cls,
            strategy_params=strategy_params,
            trial_id=trial_id,
            initial_state=initial_state,
            events_batch_size=events_batch_size,
        )

    checkpoints_dir = _resolve_checkpoints_dir(requested_run_id)
    cp_path = latest_checkpoint_path(checkpoints_dir)
    if cp_path is None:
        print(
            f"[resume] run_id={requested_run_id} requested but no checkpoint found in {checkpoints_dir}; "
            "starting from scratch."
        )
        return execute_and_persist(
            config=config,
            strategy_cls=strategy_cls,
            strategy_params=strategy_params,
            trial_id=trial_id,
            initial_state=initial_state,
            events_batch_size=events_batch_size,
        )

    # Patch a fresh EngineConfig so we don't mutate the caller's object.
    patched = EngineConfig(**config.__dict__)
    patched.resume_from_checkpoint = cp_path
    patched.checkpoints_dir = checkpoints_dir
    print(f"[resume] run_id={requested_run_id} from {cp_path}")
    _emit_resume_audit(requested_run_id, cp_path)
    return execute_and_persist(
        config=patched,
        strategy_cls=strategy_cls,
        strategy_params=strategy_params,
        trial_id=trial_id,
        initial_state=initial_state,
        events_batch_size=events_batch_size,
    )


def execute_and_persist(
    config: EngineConfig,
    strategy_cls: Type[StrategyBase],
    strategy_params: Optional[Dict] = None,
    trial_id: Optional[int] = None,
    initial_state: Optional[Dict] = None,
    events_batch_size: int = 5000,
) -> BacktestResult:
    init_db(config.db_path)
    strategy_params = strategy_params or {}
    run_cfg = EngineConfig(**config.__dict__)
    run_cfg.initial_state = initial_state or config.initial_state
    run_id = create_run(
        db_path=run_cfg.db_path,
        strategy_name=strategy_cls.name,
        symbol=run_cfg.symbol,
        interval=run_cfg.interval,
        start_ts=run_cfg.start_ts,
        end_ts=run_cfg.end_ts,
        initial_cash=run_cfg.initial_cash,
        fee_rate=run_cfg.fee_rate,
        slippage_bps=run_cfg.slippage_bps,
        config={
            "engine": run_cfg.__dict__,
            "strategy": strategy_params,
        },
    )
    try:
        result = run_backtest(
            config=run_cfg,
            strategy_cls=strategy_cls,
            strategy_params=strategy_params,
            run_id=run_id,
            trial_id=trial_id,
        )
        persist_run_events(
            run_cfg.db_path,
            run_id=run_id,
            events=result.events,
            batch_size=events_batch_size,
        )
        if _parquet_events_enabled() and (run_cfg.events_mode or "full").strip().lower() == "full":
            try:
                from backtest.events_parquet import dump_events_to_parquet  # local import

                target_dir = run_report_dir("reports", run_id)
                dump_events_to_parquet(
                    output_path=os.path.join(target_dir, "events.parquet"),
                    events=result.events,
                )
            except Exception:
                # Parquet sink is best-effort and must never fail the run.
                pass
        persist_run_metrics(
            run_cfg.db_path,
            run_id=run_id,
            metrics=result.metrics,
            trial_id=trial_id,
            extra={
                "symbol": run_cfg.symbol,
                "interval": run_cfg.interval,
                "final_state": result.final_state,
            },
        )
        finish_run(run_cfg.db_path, run_id=run_id, status="completed")
        result.run_id = run_id
        return result
    except Exception:
        finish_run(run_cfg.db_path, run_id=run_id, status="failed")
        raise

