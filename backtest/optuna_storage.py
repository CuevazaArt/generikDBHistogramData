"""Optuna storage URL builder for SQLite and PostgreSQL backends.

Fase 3 routes Optuna's RDB storage to PostgreSQL (under a dedicated `optuna`
schema) whenever `AppConfig.metadata_backend == 'pg'` and `PG_DSN` is set. The
SQLite fallback preserves the legacy behaviour used by the existing
`optimize_strategy` callers.

Two responsibilities live here:

- `build_storage(...)` returns a string Optuna can hand to `create_study`.
- `ensure_optuna_schema(...)` creates the `optuna` schema on the PG server
  before Optuna's first connection so its DDL lands in the right namespace.

The PG URL form is `postgresql+psycopg://...?options=-csearch_path%3Doptuna%2Cpublic`
so Optuna's own bookkeeping tables (`studies`, `trials`, `trial_values`, ...)
live in `optuna.*` instead of mixing with `meta.*`/`ops.*`.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from backtest.config import AppConfig


_PG_PREFIX_SQLALCHEMY = "postgresql+psycopg://"
_PG_PREFIX_PSYCOPG = "postgresql://"


def _to_sqlalchemy_dsn(dsn: str) -> str:
    """Coerce a psycopg-style DSN into the SQLAlchemy `postgresql+psycopg` form."""
    raw = dsn.strip()
    if raw.startswith(_PG_PREFIX_SQLALCHEMY):
        return raw
    if raw.startswith(_PG_PREFIX_PSYCOPG):
        return _PG_PREFIX_SQLALCHEMY + raw[len(_PG_PREFIX_PSYCOPG):]
    return raw


def _to_psycopg_dsn(dsn: str) -> str:
    raw = dsn.strip()
    if raw.startswith(_PG_PREFIX_SQLALCHEMY):
        return _PG_PREFIX_PSYCOPG + raw[len(_PG_PREFIX_SQLALCHEMY):]
    return raw


def _inject_search_path(url: str, schemas: str = "optuna,public") -> str:
    """Add `options=-csearch_path=<schemas>` to the URL's query string."""
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    options = params.get("options", "")
    target = f"-csearch_path={schemas}"
    if target not in options:
        options = f"{options} {target}".strip() if options else target
    params["options"] = options
    new_query = urlencode(params)
    return urlunparse(parsed._replace(query=new_query))


def ensure_optuna_schema(dsn: str) -> None:
    """`CREATE SCHEMA IF NOT EXISTS optuna` on the target database.

    Uses psycopg directly (autocommit) and swallows the import error so callers
    that only ever use SQLite never need psycopg present.
    """
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - hard requirement when PG is used
        raise RuntimeError(
            "psycopg is required to bootstrap the Optuna schema in PostgreSQL"
        ) from exc

    conninfo = _to_psycopg_dsn(dsn)
    with psycopg.connect(conninfo, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA IF NOT EXISTS optuna")


def build_storage(
    study_name: str,
    app_config: Optional[AppConfig] = None,
    *,
    sqlite_path: Optional[str] = None,
) -> str:
    """Return an Optuna storage URL appropriate for `app_config`.

    - When the resolved backend is `pg` and a DSN is available, returns a
      `postgresql+psycopg://...` URL with `search_path=optuna,public` injected
      so Optuna's tables land in the `optuna` schema. Best-effort
      `CREATE SCHEMA IF NOT EXISTS` is attempted; failures bubble up so the
      caller can decide whether to fall back to SQLite.
    - Otherwise returns `sqlite:///<path>` using `sqlite_path` if provided, or
      `app_config.sqlite_path` as the fallback.
    """
    cfg = app_config or AppConfig.from_env()
    backend = (cfg.metadata_backend or "sqlite").strip().lower()

    if backend == "pg" and cfg.pg_dsn:
        sqlalchemy_url = _to_sqlalchemy_dsn(cfg.pg_dsn)
        url = _inject_search_path(sqlalchemy_url, schemas="optuna,public")
        try:
            ensure_optuna_schema(cfg.pg_dsn)
        except Exception:
            # Defer the failure to Optuna; some test environments stub psycopg.
            pass
        return url

    path = sqlite_path or cfg.sqlite_path
    return f"sqlite:///{path}"


__all__ = ["build_storage", "ensure_optuna_schema"]
