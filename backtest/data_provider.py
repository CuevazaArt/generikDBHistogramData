"""DataProvider abstraction over Parquet and SQLite kline storage.

Library bots, indicators and tools consume historical klines (and contribute
back derived datasets) through this thin interface so they never need to
hardcode SQLite / Parquet / Postgres details.

Two concrete backends are shipped:

- ``ParquetDataProvider``: reads from the partitioned layout under
  ``data/klines/symbol=*/interval=*/year=*/month=*/part-000.parquet`` produced
  by ``scripts/backup_klines_to_parquet.py``. When a requested window is not
  fully covered it transparently falls back to SQLite via
  :func:`db.iter_query_klines`.
- ``SQLiteDataProvider``: thin wrapper over the legacy ``db.query_klines`` /
  ``db.iter_query_klines`` helpers.

Use :func:`get_data_provider` to obtain the recommended backend. Selection
order: explicit argument > ``BACKTEST_DATA_BACKEND`` env var > parquet (if a
manifest exists) > sqlite.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

try:
    import pyarrow as pa  # type: ignore[import-not-found]
    import pyarrow.parquet as pq  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - optional dependency
    pa = None  # type: ignore[assignment]
    pq = None  # type: ignore[assignment]


KLINES_ROOT_DEFAULT = os.path.join("data", "klines")
DERIVED_ROOT_DEFAULT = os.path.join("data", "derived")
DEFAULT_DB_PATH = "klines.db"

_KLINE_FIELDS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_asset_volume",
    "num_trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore_field",
)


def _row_to_dict(symbol: str, interval: str, row: Any) -> Dict[str, Any]:
    """Normalize a SQLite kline row into the canonical candle dict."""
    return {
        "symbol": symbol,
        "interval": interval,
        "open_time": int(row[2]),
        "open": float(row[3]),
        "high": float(row[4]),
        "low": float(row[5]),
        "close": float(row[6]),
        "volume": float(row[7]),
        "close_time": int(row[8]) if row[8] is not None else None,
        "quote_asset_volume": float(row[9]) if row[9] is not None else None,
        "num_trades": int(row[10]) if row[10] is not None else 0,
        "taker_buy_base": float(row[11]) if row[11] is not None else None,
        "taker_buy_quote": float(row[12]) if row[12] is not None else None,
        "ignore_field": str(row[13]) if len(row) > 13 and row[13] is not None else "",
    }


class DataProvider:
    """Abstract DataProvider interface.

    Concrete subclasses must implement at least :meth:`list_symbols`,
    :meth:`list_intervals` and :meth:`load_candles`. Default implementations
    are provided for the rest so subclasses can opt-in incrementally.
    """

    name: str = "base"

    def list_symbols(self) -> List[str]:
        raise NotImplementedError

    def list_intervals(self, symbol: str) -> List[str]:
        raise NotImplementedError

    def load_candles(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError

    def iter_candles(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        fetch_size: int = 10000,
    ) -> Iterator[Dict[str, Any]]:
        for row in self.load_candles(symbol, interval, start_ts, end_ts):
            yield row

    def has_derived(self, name: str) -> bool:
        return Path(DERIVED_ROOT_DEFAULT, name).exists()

    def load_derived(
        self,
        name: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        path = Path(DERIVED_ROOT_DEFAULT, name)
        if not path.exists() or pq is None:
            return None
        files = sorted(path.rglob("*.parquet"))
        if not files:
            return None
        out: List[Dict[str, Any]] = []
        for fp in files:
            table = pq.read_table(str(fp))
            data = table.to_pylist()
            for entry in data:
                ts = entry.get("open_time")
                if isinstance(ts, (int, float)):
                    if start_ts is not None and ts < start_ts:
                        continue
                    if end_ts is not None and ts > end_ts:
                        continue
                out.append(entry)
        return out

    def contribute_derived(
        self,
        name: str,
        rows: Iterable[Dict[str, Any]],
        schema: Optional[Dict[str, str]] = None,
        derived_root: str = DERIVED_ROOT_DEFAULT,
    ) -> Path:
        """Persist a derived dataset under ``data/derived/<name>/part-000.parquet``.

        ``schema`` is an optional mapping of column name to pyarrow type alias
        (``"int64"``, ``"float64"``, ``"string"``). When omitted the schema is
        inferred from the first row.
        """
        if pa is None or pq is None:
            raise RuntimeError("pyarrow is required to contribute derived datasets")
        target_dir = Path(derived_root, name)
        target_dir.mkdir(parents=True, exist_ok=True)
        rows_list = list(rows)
        if not rows_list:
            raise ValueError("contribute_derived requires at least one row")
        columns: Dict[str, List[Any]] = {key: [] for key in rows_list[0].keys()}
        for entry in rows_list:
            for key in columns:
                columns[key].append(entry.get(key))
        if schema:
            pa_schema = pa.schema([(k, _resolve_pa_type(v)) for k, v in schema.items()])
            table = pa.table(columns, schema=pa_schema)
        else:
            table = pa.table(columns)
        out_path = target_dir / "part-000.parquet"
        pq.write_table(table, str(out_path), compression="zstd")
        return out_path


def _resolve_pa_type(alias: str) -> Any:
    aliases = {
        "int64": pa.int64(),
        "int32": pa.int32(),
        "float64": pa.float64(),
        "float32": pa.float32(),
        "string": pa.string(),
        "bool": pa.bool_(),
    }
    if alias not in aliases:
        raise ValueError(f"unsupported pyarrow type alias: {alias}")
    return aliases[alias]


class ParquetDataProvider(DataProvider):
    """Read candles from the partitioned Parquet layout under ``data/klines/``.

    Falls back to SQLite via :func:`db.iter_query_klines` when the requested
    window is not fully materialised on disk.
    """

    name = "parquet"

    def __init__(
        self,
        root: str = KLINES_ROOT_DEFAULT,
        fallback_db: Optional[str] = DEFAULT_DB_PATH,
    ) -> None:
        self.root = Path(root)
        self.fallback_db = fallback_db
        self._manifest_cache: Optional[Dict[str, Any]] = None

    def _manifest(self) -> Dict[str, Any]:
        if self._manifest_cache is not None:
            return self._manifest_cache
        manifest_path = self.root / "_manifest.json"
        if not manifest_path.exists():
            self._manifest_cache = {"series": []}
            return self._manifest_cache
        with manifest_path.open("r", encoding="utf-8") as fh:
            self._manifest_cache = json.load(fh)
        return self._manifest_cache

    def list_symbols(self) -> List[str]:
        manifest = self._manifest()
        names = sorted({entry["symbol"] for entry in manifest.get("series", [])})
        if names:
            return names
        if not self.root.exists():
            return []
        return sorted({p.name.split("=")[1] for p in self.root.glob("symbol=*")})

    def list_intervals(self, symbol: str) -> List[str]:
        manifest = self._manifest()
        listed = sorted(
            {entry["interval"] for entry in manifest.get("series", []) if entry["symbol"] == symbol}
        )
        if listed:
            return listed
        base = self.root / f"symbol={symbol}"
        if not base.exists():
            return []
        return sorted({p.name.split("=")[1] for p in base.glob("interval=*")})

    def _partition_paths(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int],
        end_ts: Optional[int],
    ) -> List[Path]:
        base = self.root / f"symbol={symbol}" / f"interval={interval}"
        if not base.exists():
            return []
        all_files = sorted(base.glob("year=*/month=*/part-*.parquet"))
        if start_ts is None and end_ts is None:
            return all_files
        keep: List[Path] = []
        for fp in all_files:
            year, month = _parse_partition(fp)
            if year is None or month is None:
                keep.append(fp)
                continue
            month_start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)
            if month == 12:
                next_start = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            else:
                next_start = int(datetime(year, month + 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            month_end = next_start - 1
            if start_ts is not None and month_end < start_ts:
                continue
            if end_ts is not None and month_start > end_ts:
                continue
            keep.append(fp)
        return keep

    def _missing_window(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int],
        end_ts: Optional[int],
        paths: List[Path],
    ) -> bool:
        if start_ts is None or end_ts is None or not paths:
            return not paths
        covered_start = None
        covered_end = None
        for fp in paths:
            year, month = _parse_partition(fp)
            if year is None or month is None:
                continue
            month_start = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)
            if month == 12:
                next_start = int(datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            else:
                next_start = int(datetime(year, month + 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
            month_end = next_start - 1
            covered_start = month_start if covered_start is None else min(covered_start, month_start)
            covered_end = month_end if covered_end is None else max(covered_end, month_end)
        if covered_start is None or covered_end is None:
            return True
        return start_ts < covered_start or end_ts > covered_end

    def load_candles(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        paths = self._partition_paths(symbol, interval, start_ts, end_ts)
        if not paths or pq is None:
            return self._load_from_sqlite(symbol, interval, start_ts, end_ts)
        if self._missing_window(symbol, interval, start_ts, end_ts, paths):
            return self._load_from_sqlite(symbol, interval, start_ts, end_ts)
        out: List[Dict[str, Any]] = []
        for fp in paths:
            table = pq.read_table(str(fp))
            data = table.to_pydict()
            n = len(data["open_time"])
            for i in range(n):
                ts = int(data["open_time"][i])
                if start_ts is not None and ts < start_ts:
                    continue
                if end_ts is not None and ts > end_ts:
                    continue
                out.append(
                    {
                        "symbol": symbol,
                        "interval": interval,
                        "open_time": ts,
                        "open": float(data["open"][i]),
                        "high": float(data["high"][i]),
                        "low": float(data["low"][i]),
                        "close": float(data["close"][i]),
                        "volume": float(data["volume"][i]),
                        "close_time": int(data["close_time"][i]) if data["close_time"][i] is not None else None,
                        "quote_asset_volume": (
                            float(data["quote_asset_volume"][i])
                            if data["quote_asset_volume"][i] is not None
                            else None
                        ),
                        "num_trades": int(data["num_trades"][i]) if data["num_trades"][i] is not None else 0,
                        "taker_buy_base": (
                            float(data["taker_buy_base"][i]) if data["taker_buy_base"][i] is not None else None
                        ),
                        "taker_buy_quote": (
                            float(data["taker_buy_quote"][i]) if data["taker_buy_quote"][i] is not None else None
                        ),
                        "ignore_field": (
                            str(data["ignore_field"][i]) if data["ignore_field"][i] is not None else ""
                        ),
                    }
                )
        out.sort(key=lambda r: r["open_time"])
        return out

    def iter_candles(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        fetch_size: int = 10000,
    ) -> Iterator[Dict[str, Any]]:
        paths = self._partition_paths(symbol, interval, start_ts, end_ts)
        if not paths or pq is None or self._missing_window(symbol, interval, start_ts, end_ts, paths):
            yield from self._iter_from_sqlite(symbol, interval, start_ts, end_ts)
            return
        for fp in paths:
            table = pq.read_table(str(fp))
            data = table.to_pydict()
            n = len(data["open_time"])
            for i in range(n):
                ts = int(data["open_time"][i])
                if start_ts is not None and ts < start_ts:
                    continue
                if end_ts is not None and ts > end_ts:
                    continue
                yield {
                    "symbol": symbol,
                    "interval": interval,
                    "open_time": ts,
                    "open": float(data["open"][i]),
                    "high": float(data["high"][i]),
                    "low": float(data["low"][i]),
                    "close": float(data["close"][i]),
                    "volume": float(data["volume"][i]),
                    "close_time": int(data["close_time"][i]) if data["close_time"][i] is not None else None,
                    "quote_asset_volume": (
                        float(data["quote_asset_volume"][i])
                        if data["quote_asset_volume"][i] is not None
                        else None
                    ),
                    "num_trades": int(data["num_trades"][i]) if data["num_trades"][i] is not None else 0,
                    "taker_buy_base": (
                        float(data["taker_buy_base"][i]) if data["taker_buy_base"][i] is not None else None
                    ),
                    "taker_buy_quote": (
                        float(data["taker_buy_quote"][i]) if data["taker_buy_quote"][i] is not None else None
                    ),
                    "ignore_field": (
                        str(data["ignore_field"][i]) if data["ignore_field"][i] is not None else ""
                    ),
                }

    def _load_from_sqlite(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int],
        end_ts: Optional[int],
    ) -> List[Dict[str, Any]]:
        if not self.fallback_db or not Path(self.fallback_db).exists():
            return []
        from db import query_klines

        rows = query_klines(self.fallback_db, symbol=symbol, interval=interval, start_ts=start_ts, end_ts=end_ts)
        return [_row_to_dict(symbol, interval, r) for r in rows]

    def _iter_from_sqlite(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int],
        end_ts: Optional[int],
    ) -> Iterator[Dict[str, Any]]:
        if not self.fallback_db or not Path(self.fallback_db).exists():
            return
        from db import iter_query_klines

        for row in iter_query_klines(
            self.fallback_db,
            symbol=symbol,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
        ):
            yield _row_to_dict(symbol, interval, row)


class SQLiteDataProvider(DataProvider):
    """DataProvider implementation backed exclusively by SQLite."""

    name = "sqlite"

    def __init__(self, db_path: str = DEFAULT_DB_PATH) -> None:
        self.db_path = db_path

    def list_symbols(self) -> List[str]:
        if not Path(self.db_path).exists():
            return []
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute("SELECT DISTINCT symbol FROM klines ORDER BY symbol ASC")
            return [str(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()

    def list_intervals(self, symbol: str) -> List[str]:
        if not Path(self.db_path).exists():
            return []
        import sqlite3

        conn = sqlite3.connect(self.db_path)
        try:
            cur = conn.execute(
                "SELECT DISTINCT interval FROM klines WHERE symbol=? ORDER BY interval ASC",
                (symbol,),
            )
            return [str(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()

    def load_candles(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        if not Path(self.db_path).exists():
            return []
        from db import query_klines

        rows = query_klines(self.db_path, symbol=symbol, interval=interval, start_ts=start_ts, end_ts=end_ts)
        return [_row_to_dict(symbol, interval, r) for r in rows]

    def iter_candles(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        fetch_size: int = 10000,
    ) -> Iterator[Dict[str, Any]]:
        if not Path(self.db_path).exists():
            return
        from db import iter_query_klines

        for row in iter_query_klines(
            self.db_path,
            symbol=symbol,
            interval=interval,
            start_ts=start_ts,
            end_ts=end_ts,
            fetch_size=fetch_size,
        ):
            yield _row_to_dict(symbol, interval, row)


def _parse_partition(path: Path) -> tuple[Optional[int], Optional[int]]:
    year = month = None
    for part in path.parts:
        if part.startswith("year="):
            try:
                year = int(part.split("=", 1)[1])
            except ValueError:
                year = None
        elif part.startswith("month="):
            try:
                month = int(part.split("=", 1)[1])
            except ValueError:
                month = None
    return year, month


def _resolve_backend(backend: Optional[str]) -> str:
    if backend:
        return backend.strip().lower()
    env_val = os.getenv("BACKTEST_DATA_BACKEND")
    if env_val:
        return env_val.strip().lower()
    manifest = Path(KLINES_ROOT_DEFAULT) / "_manifest.json"
    return "parquet" if manifest.exists() else "sqlite"


def get_data_provider(
    backend: Optional[str] = None,
    db_path: Optional[str] = None,
    klines_root: Optional[str] = None,
) -> DataProvider:
    """Factory returning the configured DataProvider implementation."""
    selected = _resolve_backend(backend)
    db_target = db_path or DEFAULT_DB_PATH
    root_target = klines_root or KLINES_ROOT_DEFAULT
    if selected == "sqlite":
        return SQLiteDataProvider(db_path=db_target)
    if selected == "parquet":
        return ParquetDataProvider(root=root_target, fallback_db=db_target)
    raise ValueError(f"unsupported data backend: {selected}")
