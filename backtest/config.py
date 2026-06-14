"""Centralised application configuration sourced from environment variables.

Kept deliberately small: a single dataclass with sensible defaults. Code that
needs to know which backend or root directory to use should call
`AppConfig.from_env()` rather than reading os.environ directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional


_TRUTHY = {"1", "true", "yes", "on"}


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


@dataclass(frozen=True)
class AppConfig:
    pg_dsn: str | None
    metadata_backend: str
    data_root: str
    sqlite_path: str
    engine_kind: str
    events_mode: str

    @staticmethod
    def from_env() -> "AppConfig":
        pg_dsn = os.getenv("PG_DSN")
        default_backend = "sqlite" if pg_dsn is None else "pg"
        metadata_backend = os.getenv("BACKTEST_METADATA_BACKEND", default_backend).strip().lower()
        if metadata_backend not in {"pg", "sqlite"}:
            raise ValueError(
                f"BACKTEST_METADATA_BACKEND must be 'pg' or 'sqlite', got {metadata_backend!r}"
            )

        events_mode = os.getenv("BACKTEST_EVENTS_MODE", "lite").strip().lower()
        if events_mode not in {"full", "lite", "minimal"}:
            raise ValueError(
                f"BACKTEST_EVENTS_MODE must be one of full|lite|minimal, got {events_mode!r}"
            )

        engine_kind = os.getenv("BACKTEST_ENGINE_KIND", "python").strip().lower()
        if engine_kind not in {"python", "rust"}:
            raise ValueError(
                f"BACKTEST_ENGINE_KIND must be 'python' or 'rust', got {engine_kind!r}"
            )

        return AppConfig(
            pg_dsn=pg_dsn,
            metadata_backend=metadata_backend,
            data_root=os.getenv("BACKTEST_DATA_ROOT", "data"),
            sqlite_path=os.getenv("BACKTEST_SQLITE_PATH", "klines.db"),
            engine_kind=engine_kind,
            events_mode=events_mode,
        )


__all__ = ["AppConfig"]
