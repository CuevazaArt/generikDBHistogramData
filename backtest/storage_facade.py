"""Backend-agnostic facade over the storage layer.

`get_storage(config)` returns an object that exposes the union of the methods
implemented by `backtest.storage` (SQLite) and `backtest.storage_pg`
(PostgreSQL + Parquet). The facade is purely additive: existing callers keep
talking directly to `backtest.storage`; new code should opt in by calling
`get_storage(AppConfig.from_env())`.

Both backends keep the same Python-level signatures wherever possible. Where
the underlying schema differs (e.g. SQLite stores events in a table, PG stores
them in Parquet), the wrappers translate the call site's intent transparently.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Protocol, runtime_checkable

from backtest import storage as sqlite_storage
from backtest import storage_pg as pg_storage
from backtest.config import AppConfig
from backtest.storage_paths import StoragePaths


@runtime_checkable
class StorageBackend(Protocol):
    """Common interface implemented by both the SQLite and PG backends."""

    kind: str

    def create_run(
        self,
        *,
        strategy: str,
        symbol: str,
        interval: str,
        start_ts: int | None,
        end_ts: int | None,
        initial_cash: float,
        fee_rate: float,
        slippage_bps: float,
        config: Dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        engine_kind: str = "python",
        engine_version: str = "0.0.0",
        strategy_params: Dict[str, Any] | None = None,
    ) -> int: ...

    def finish_run(self, run_id: int, status: str = "completed") -> None: ...

    def persist_run_events(
        self,
        run_id: int,
        events: Iterable[Dict[str, Any]],
        *,
        seq: int = 0,
    ) -> str | None: ...

    def persist_run_metrics(
        self,
        run_id: int,
        metrics: Dict[str, float],
        trial_id: int | None = None,
        extra: Dict[str, Any] | None = None,
    ) -> None: ...

    def save_trial(
        self,
        *,
        study_name: str,
        optuna_trial_num: int,
        state: str,
        objective: float | None,
        params: Dict[str, Any],
        started_at: str | None = None,
        finished_at: str | None = None,
        run_id: int | None = None,
    ) -> int: ...

    def save_trial_metrics(self, trial_id: int, metrics: Dict[str, float]) -> None: ...

    def list_runs(self, limit: int = 20) -> List[Any]: ...

    def run_equity_curve(self, run_id: int) -> List[Any]: ...

    def trial_objectives(self, study_name: str, limit: int = 500) -> List[Any]: ...

    def run_signal_events(self, run_id: int) -> List[Any]: ...

    def run_descriptor(self, run_id: int) -> Dict[str, Any] | None: ...

    def run_events(self, run_id: int) -> List[Any]: ...

    def study_trials(self, study_name: str, limit: int = 10_000) -> List[Any]: ...

    def list_top_bt_trials(self, study_name: str, limit: int = 10) -> List[Any]: ...

    def get_bt_run_metrics(self, run_id: int) -> Dict[str, float]: ...

    def get_bt_recent_events(self, run_id: int, limit: int = 30) -> List[Any]: ...


class SqliteBackend:
    """Adapter over the legacy `backtest.storage` module."""

    kind = "sqlite"

    def __init__(self, db_path: str) -> None:
        self.db_path = db_path

    def create_run(
        self,
        *,
        strategy: str,
        symbol: str,
        interval: str,
        start_ts: int | None,
        end_ts: int | None,
        initial_cash: float,
        fee_rate: float,
        slippage_bps: float,
        config: Dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        engine_kind: str = "python",
        engine_version: str = "0.0.0",
        strategy_params: Dict[str, Any] | None = None,
    ) -> int:
        # The SQLite layer does not consume `idempotency_key` / `engine_kind` /
        # `engine_version`; they are silently ignored here. The PG backend uses
        # them; both behave identically for callers that don't rely on
        # idempotency.
        return sqlite_storage.create_run(
            db_path=self.db_path,
            strategy_name=strategy,
            symbol=symbol,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
            initial_cash=initial_cash,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            config=config,
        )

    def finish_run(self, run_id: int, status: str = "completed") -> None:
        sqlite_storage.finish_run(self.db_path, run_id=run_id, status=status)

    def persist_run_events(
        self,
        run_id: int,
        events: Iterable[Dict[str, Any]],
        *,
        seq: int = 0,
    ) -> str | None:
        sqlite_storage.persist_run_events(self.db_path, run_id=run_id, events=list(events))
        return None

    def persist_run_metrics(
        self,
        run_id: int,
        metrics: Dict[str, float],
        trial_id: int | None = None,
        extra: Dict[str, Any] | None = None,
    ) -> None:
        sqlite_storage.persist_run_metrics(
            self.db_path, run_id=run_id, metrics=metrics, trial_id=trial_id, extra=extra
        )

    def save_trial(
        self,
        *,
        study_name: str,
        optuna_trial_num: int,
        state: str,
        objective: float | None,
        params: Dict[str, Any],
        started_at: str | None = None,
        finished_at: str | None = None,
        run_id: int | None = None,
    ) -> int:
        return sqlite_storage.save_trial(
            db_path=self.db_path,
            study_name=study_name,
            trial_number=optuna_trial_num,
            state=state,
            objective=objective,
            params=params,
            started_at=started_at or "",
            finished_at=finished_at,
            duration_sec=None,
        )

    def save_trial_metrics(self, trial_id: int, metrics: Dict[str, float]) -> None:
        sqlite_storage.save_trial_metrics(self.db_path, trial_id=trial_id, metrics=metrics)

    def list_runs(self, limit: int = 20) -> List[Any]:
        return sqlite_storage.list_runs(self.db_path, limit=limit)

    def run_equity_curve(self, run_id: int) -> List[Any]:
        return sqlite_storage.run_equity_curve(self.db_path, run_id=run_id)

    def trial_objectives(self, study_name: str, limit: int = 500) -> List[Any]:
        return sqlite_storage.trial_objectives(self.db_path, study_name=study_name, limit=limit)

    def run_signal_events(self, run_id: int) -> List[Any]:
        return sqlite_storage.run_signal_events(self.db_path, run_id=run_id)

    def run_descriptor(self, run_id: int) -> Dict[str, Any] | None:
        return sqlite_storage.run_descriptor(self.db_path, run_id=run_id)

    def run_events(self, run_id: int) -> List[Any]:
        return sqlite_storage.run_events(self.db_path, run_id=run_id)

    def study_trials(self, study_name: str, limit: int = 10_000) -> List[Any]:
        return sqlite_storage.study_trials(self.db_path, study_name=study_name, limit=limit)

    def list_top_bt_trials(self, study_name: str, limit: int = 10) -> List[Any]:
        return sqlite_storage.top_trials(self.db_path, study_name=study_name, limit=limit)

    def get_bt_run_metrics(self, run_id: int) -> Dict[str, float]:
        from db import get_bt_run_metrics as _get
        return _get(self.db_path, run_id=run_id)

    def get_bt_recent_events(self, run_id: int, limit: int = 30) -> List[Any]:
        from db import get_bt_recent_events as _get
        return _get(self.db_path, run_id=run_id, limit=limit)


class PgBackend:
    """Adapter over `backtest.storage_pg`."""

    kind = "pg"

    def __init__(self, dsn: str, storage_paths: StoragePaths | None = None) -> None:
        self.dsn = dsn
        self.paths = storage_paths or StoragePaths()

    def create_run(
        self,
        *,
        strategy: str,
        symbol: str,
        interval: str,
        start_ts: int | None,
        end_ts: int | None,
        initial_cash: float,
        fee_rate: float,
        slippage_bps: float,
        config: Dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        engine_kind: str = "python",
        engine_version: str = "0.0.0",
        strategy_params: Dict[str, Any] | None = None,
    ) -> int:
        return pg_storage.create_run(
            self.dsn,
            strategy=strategy,
            symbol=symbol,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
            initial_cash=initial_cash,
            fee_rate=fee_rate,
            slippage_bps=slippage_bps,
            config=config,
            idempotency_key=idempotency_key,
            engine_kind=engine_kind,
            engine_version=engine_version,
            strategy_params=strategy_params,
            storage_paths=self.paths,
        )

    def finish_run(self, run_id: int, status: str = "completed") -> None:
        pg_storage.finish_run(self.dsn, run_id=run_id, status=status)

    def persist_run_events(
        self,
        run_id: int,
        events: Iterable[Dict[str, Any]],
        *,
        seq: int = 0,
    ) -> str | None:
        return pg_storage.persist_run_events(
            self.dsn, run_id=run_id, events=events, seq=seq, storage_paths=self.paths
        )

    def persist_run_metrics(
        self,
        run_id: int,
        metrics: Dict[str, float],
        trial_id: int | None = None,
        extra: Dict[str, Any] | None = None,
    ) -> None:
        pg_storage.persist_run_metrics(
            self.dsn, run_id=run_id, metrics=metrics, trial_id=trial_id, extra=extra
        )

    def save_trial(
        self,
        *,
        study_name: str,
        optuna_trial_num: int,
        state: str,
        objective: float | None,
        params: Dict[str, Any],
        started_at: str | None = None,
        finished_at: str | None = None,
        run_id: int | None = None,
    ) -> int:
        return pg_storage.save_trial(
            self.dsn,
            study_name=study_name,
            optuna_trial_num=optuna_trial_num,
            state=state,
            objective=objective,
            params=params,
            started_at=started_at,
            finished_at=finished_at,
            run_id=run_id,
        )

    def save_trial_metrics(self, trial_id: int, metrics: Dict[str, float]) -> None:
        pg_storage.save_trial_metrics(self.dsn, trial_id=trial_id, metrics=metrics)

    def list_runs(self, limit: int = 20) -> List[Any]:
        return pg_storage.list_runs(self.dsn, limit=limit)

    def run_equity_curve(self, run_id: int) -> List[Any]:
        return pg_storage.run_equity_curve(self.dsn, run_id=run_id, storage_paths=self.paths)

    def trial_objectives(self, study_name: str, limit: int = 500) -> List[Any]:
        return pg_storage.trial_objectives(self.dsn, study_name=study_name, limit=limit)

    def run_signal_events(self, run_id: int) -> List[Any]:
        return pg_storage.run_signal_events(self.dsn, run_id=run_id, storage_paths=self.paths)

    def run_descriptor(self, run_id: int) -> Dict[str, Any] | None:
        return pg_storage.run_descriptor(self.dsn, run_id=run_id)

    def run_events(self, run_id: int) -> List[Any]:
        return pg_storage.run_events(self.dsn, run_id=run_id, storage_paths=self.paths)

    def study_trials(self, study_name: str, limit: int = 10_000) -> List[Any]:
        return pg_storage.study_trials(self.dsn, study_name=study_name, limit=limit)

    def list_top_bt_trials(self, study_name: str, limit: int = 10) -> List[Any]:
        return pg_storage.list_top_bt_trials(self.dsn, study_name=study_name, limit=limit)

    def get_bt_run_metrics(self, run_id: int) -> Dict[str, float]:
        return pg_storage.get_bt_run_metrics(self.dsn, run_id=run_id)

    def get_bt_recent_events(self, run_id: int, limit: int = 30) -> List[Any]:
        return pg_storage.get_bt_recent_events(
            self.dsn, run_id=run_id, limit=limit, storage_paths=self.paths
        )


def get_storage(config: AppConfig | None = None) -> StorageBackend:
    """Return the backend selected by `config`.

    If `config` is None, falls back to `AppConfig.from_env()`. The choice is
    driven by `BACKTEST_METADATA_BACKEND` (`pg` or `sqlite`); the PG backend
    requires `PG_DSN` to be set.
    """
    cfg = config or AppConfig.from_env()
    if cfg.metadata_backend == "pg":
        if not cfg.pg_dsn:
            raise RuntimeError(
                "BACKTEST_METADATA_BACKEND=pg but PG_DSN is not set."
            )
        return PgBackend(dsn=cfg.pg_dsn, storage_paths=StoragePaths(cfg.data_root))
    return SqliteBackend(db_path=cfg.sqlite_path)


__all__ = ["PgBackend", "SqliteBackend", "StorageBackend", "get_storage"]
