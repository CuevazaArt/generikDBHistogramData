"""Utilities to download Binance klines either via REST API or monthly zip files.

Features:
- `download_klines_api` streams historical klines using Binance REST `/api/v3/klines`.
- `download_klines_zip` downloads monthly zip files from data.binance.vision and yields rows.

Rows are yielded as tuples matching the DB schema in `db.py`.
"""
from difflib import get_close_matches
from typing import Dict, Iterator, List, Optional, Tuple
import csv
import io
import random
import time
import zipfile

import requests  # type: ignore[import-untyped]


def _is_transient_status(status: int) -> bool:
    return status in (408, 425, 429, 500, 502, 503, 504)


def _retry_after_seconds(response: "requests.Response", base: float, attempt: int) -> float:
    header = response.headers.get("Retry-After") if response is not None else None
    if header:
        try:
            return max(0.0, float(header))
        except (TypeError, ValueError):
            pass
    # Exponential backoff with capped jitter
    delay = base * (2 ** attempt)
    jitter = random.uniform(0.0, min(1.0, delay * 0.25))
    return min(60.0, delay) + jitter


class BinanceDownloader:
    BASE_API = "https://api.binance.com"
    BASE_ALPHA = "https://www.binance.com"
    DATA_ZIP_BASE = "https://data.binance.vision/data/spot/monthly/klines"
    ALPHA_TOKEN_LIST_PATH = "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
    ALPHA_KLINES_PATH = "/bapi/defi/v1/public/alpha-trade/klines"
    COMMON_QUOTE_ASSETS = (
        "USDT",
        "USDC",
        "FDUSD",
        "BUSD",
        "BTC",
        "ETH",
        "BNB",
        "TRY",
        "EUR",
        "BRL",
        "DAI",
        "TUSD",
    )

    def __init__(self, session: Optional[requests.Session] = None):
        self.s = session or requests.Session()

    def download_klines_api(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        limit: int = 1000,
        sleep_on_rate_limit: float = 0.5,
        max_retries: int = 6,
    ) -> Iterator[Tuple]:
        """Yield kline rows (tuples) from Binance REST API.

        Retries with exponential backoff + jitter on transient errors (429/5xx
        and network failures). Honors a `Retry-After` header when present.
        """
        path = f"/api/v3/klines"
        url = self.BASE_API + path
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ts is not None:
            params["startTime"] = int(start_ts)
        if end_ts is not None:
            params["endTime"] = int(end_ts)

        while True:
            attempt = 0
            r = None
            while True:
                try:
                    r = self.s.get(url, params=params, timeout=30)
                except requests.RequestException:
                    if attempt >= max_retries:
                        raise
                    time.sleep(_retry_after_seconds(None, sleep_on_rate_limit, attempt))
                    attempt += 1
                    continue
                if _is_transient_status(r.status_code):
                    if attempt >= max_retries:
                        r.raise_for_status()
                    time.sleep(_retry_after_seconds(r, sleep_on_rate_limit, attempt))
                    attempt += 1
                    continue
                break
            r.raise_for_status()
            data = r.json()
            if not data:
                break
            for item in data:
                row = (
                    int(item[0]),
                    float(item[1]),
                    float(item[2]),
                    float(item[3]),
                    float(item[4]),
                    float(item[5]),
                    int(item[6]),
                    float(item[7]) if item[7] != "" else None,
                    int(item[8]),
                    float(item[9]) if item[9] != "" else None,
                    float(item[10]) if item[10] != "" else None,
                    item[11] if len(item) > 11 else "",
                )
                yield row

            # paginate: set next startTime to last open_time + 1ms
            last_open = int(data[-1][0])
            next_start = last_open + 1
            params["startTime"] = next_start
            if end_ts is not None and next_start > end_ts:
                break

    def get_alpha_token_list(self) -> List[Dict]:
        """Return token metadata from Binance Alpha Token List endpoint."""
        url = self.BASE_ALPHA + self.ALPHA_TOKEN_LIST_PATH
        try:
            r = self.s.get(url, timeout=30)
        except requests.RequestException as exc:
            raise RuntimeError(f"Alpha token list request failed: {exc}") from exc
        if r.status_code != 200:
            body = r.text[:300] if r.text else ""
            raise RuntimeError(f"Alpha token list HTTP {r.status_code}: {body}")
        try:
            payload = r.json()
        except Exception as exc:
            raise RuntimeError("Alpha token list response is not valid JSON") from exc
        if str(payload.get("code")) != "000000" or not payload.get("success", False):
            raise RuntimeError(
                f"Alpha token list API error: code={payload.get('code')} message={payload.get('message')}"
            )
        data = payload.get("data")
        if not isinstance(data, list):
            raise RuntimeError("Alpha token list payload does not contain a valid data list")
        return data

    def get_alpha_exchange_info(self) -> Dict:
        """Return Alpha exchange-info (symbols + filters)."""
        url = self.BASE_ALPHA + self.ALPHA_EXCHANGE_INFO_PATH
        try:
            r = self.s.get(url, timeout=30)
        except requests.RequestException as exc:
            raise RuntimeError(f"Alpha exchange-info request failed: {exc}") from exc
        if r.status_code != 200:
            body = r.text[:300] if r.text else ""
            raise RuntimeError(f"Alpha exchange-info HTTP {r.status_code}: {body}")
        try:
            payload = r.json()
        except Exception as exc:
            raise RuntimeError("Alpha exchange-info response is not valid JSON") from exc
        if str(payload.get("code")) != "000000":
            raise RuntimeError(
                f"Alpha exchange-info API error: code={payload.get('code')} message={payload.get('message')}"
            )
        return payload.get("data") or {}

    def alpha_symbols_for_alpha_id(self, alpha_id: str) -> List[str]:
        """Return tradeable pair symbols (e.g. ALPHA_964USDC) for a given alphaId.

        Some Alpha tokens quote only against USDC, others against USDT. The
        token list does not state this; only exchange-info does. We list all
        symbols that begin with `<alphaId>` so the caller picks the right one.
        """
        target = str(alpha_id).upper()
        info = self.get_alpha_exchange_info()
        symbols = []
        for s in info.get("symbols", []):
            sym = str(s.get("symbol", "")).upper()
            if sym.startswith(target):
                symbols.append(sym)
        return symbols

    def resolve_alpha_symbol(self, symbol: str) -> str:
        """Map human symbol (e.g. PHAROSUSDT) to a tradeable Alpha pair.

        Process:
          1. Strip the requested quote suffix (USDT/USDC/...).
          2. Look up the alphaId in the token list.
          3. Cross-check exchange-info for actual tradeable pairs for that
             alphaId; if the requested quote is not tradeable, fall back to
             the first tradeable pair and emit a warning (typical: requested
             USDT but token only trades USDC, or vice versa).
        """
        requested = symbol.strip().upper()
        if requested.startswith("ALPHA_"):
            return requested
        quote = None
        for q in self.COMMON_QUOTE_ASSETS:
            if requested.endswith(q) and len(requested) > len(q):
                quote = q
                break
        if not quote:
            raise ValueError(
                f"Cannot resolve Alpha symbol for '{symbol}': unsupported/unknown quote asset suffix"
            )
        base = requested[: -len(quote)]
        tokens = self.get_alpha_token_list()
        symbol_to_alpha_id: Dict[str, str] = {}
        for token in tokens:
            token_symbol = str(token.get("symbol", "")).upper()
            alpha_id = str(token.get("alphaId", "")).upper()
            if token_symbol and alpha_id.startswith("ALPHA_"):
                symbol_to_alpha_id[token_symbol] = alpha_id
        resolved_alpha_id = symbol_to_alpha_id.get(base)
        if not resolved_alpha_id:
            suggestions = get_close_matches(base, sorted(symbol_to_alpha_id.keys()), n=5, cutoff=0.65)
            extra = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
            raise ValueError(f"Token '{base}' not found in Binance Alpha Token List.{extra}")

        # Validate against exchange-info: which quote(s) actually trade.
        try:
            tradeable = self.alpha_symbols_for_alpha_id(resolved_alpha_id)
        except Exception:
            tradeable = []
        if tradeable:
            preferred = f"{resolved_alpha_id}{quote}"
            if preferred in tradeable:
                return preferred
            fallback = tradeable[0]
            print(
                f"[alpha-resolve] {base}: requested quote '{quote}' not tradeable on Alpha; "
                f"available pairs={tradeable}; falling back to '{fallback}'.",
                flush=True,
            )
            return fallback
        # No exchange-info available, use legacy concatenation (caller may fail).
        return f"{resolved_alpha_id}{quote}"

    # Alpha API result codes that legitimately terminate the stream (not errors).
    # -1000 / "No records found" is returned once startTime is past the last
    # available bar. Extend this set as Binance documents new sentinels.
    ALPHA_END_OF_STREAM_CODES = frozenset({"-1000"})
    ALPHA_END_OF_STREAM_MESSAGES = (
        "No records found",
        "No data",
    )
    # Codes documented as fatal (do not retry). All other non-000000 codes are
    # treated as transient and retried with backoff.
    # -1121 = Invalid symbol (e.g. wrong quote asset for an Alpha pair).
    ALPHA_FATAL_CODES = frozenset({"-1121", "-2008", "-2011"})
    ALPHA_EXCHANGE_INFO_PATH = "/bapi/defi/v1/public/alpha-trade/get-exchange-info"

    def download_klines_alpha_api(
        self,
        symbol: str,
        interval: str,
        start_ts: Optional[int] = None,
        end_ts: Optional[int] = None,
        limit: int = 1000,
        sleep_on_rate_limit: float = 0.5,
        max_retries: int = 6,
        request_timeout: float = 30.0,
    ) -> Iterator[Tuple]:
        """Yield kline rows from Binance Alpha Klines endpoint.

        Hardened against:
          - HTTP 429 (rate limit) -> respects Retry-After + exponential backoff
          - HTTP 5xx / network exceptions -> retries with jitter, max_retries
          - Empty / malformed JSON payload -> retry up to max_retries
          - Alpha sentinel codes signaling end-of-stream -> graceful break
          - Alpha fatal codes (invalid symbol, etc.) -> raise immediately
        """
        alpha_symbol = symbol.strip().upper()
        if limit > 1500:
            limit = 1500
        url = self.BASE_ALPHA + self.ALPHA_KLINES_PATH
        params: Dict[str, int | str] = {"symbol": alpha_symbol, "interval": interval, "limit": int(limit)}
        if start_ts is not None:
            params["startTime"] = int(start_ts)
        if end_ts is not None:
            params["endTime"] = int(end_ts)

        while True:
            attempt = 0
            payload: Optional[Dict[str, Any]] = None
            last_error: Optional[str] = None
            while attempt <= max_retries:
                try:
                    r = self.s.get(url, params=params, timeout=request_timeout)
                except requests.RequestException as exc:
                    last_error = f"network error: {exc}"
                    if attempt >= max_retries:
                        raise RuntimeError(
                            f"Alpha klines exhausted retries for {alpha_symbol}: {last_error}"
                        ) from exc
                    time.sleep(_retry_after_seconds(None, sleep_on_rate_limit, attempt))
                    attempt += 1
                    continue

                if r.status_code == 429 or _is_transient_status(r.status_code):
                    last_error = f"HTTP {r.status_code}"
                    if attempt >= max_retries:
                        body = r.text[:300] if r.text else ""
                        raise RuntimeError(
                            f"Alpha klines HTTP {r.status_code} for {alpha_symbol} (max retries): {body}"
                        )
                    time.sleep(_retry_after_seconds(r, sleep_on_rate_limit, attempt))
                    attempt += 1
                    continue

                if r.status_code != 200:
                    body = r.text[:300] if r.text else ""
                    raise RuntimeError(f"Alpha klines HTTP {r.status_code} for {alpha_symbol}: {body}")

                try:
                    payload = r.json()
                except Exception as exc:
                    last_error = "invalid JSON"
                    if attempt >= max_retries:
                        raise RuntimeError(
                            f"Alpha klines response is not valid JSON for {alpha_symbol}: {exc}"
                        ) from exc
                    time.sleep(_retry_after_seconds(r, sleep_on_rate_limit, attempt))
                    attempt += 1
                    continue
                break

            if payload is None:
                raise RuntimeError(f"Alpha klines empty payload for {alpha_symbol}: {last_error}")

            code = str(payload.get("code"))
            message = str(payload.get("message", ""))
            success = bool(payload.get("success", False))

            if code != "000000" or not success:
                # End-of-stream sentinel: stop cleanly.
                if (
                    code in self.ALPHA_END_OF_STREAM_CODES
                    or any(s in message for s in self.ALPHA_END_OF_STREAM_MESSAGES)
                ):
                    break
                # Fatal: raise.
                if code in self.ALPHA_FATAL_CODES:
                    raise RuntimeError(
                        f"Alpha klines fatal error for {alpha_symbol}: code={code} message={message}"
                    )
                # Unknown non-zero code: be safe -> raise so caller learns.
                raise RuntimeError(
                    f"Alpha klines API error for {alpha_symbol}: code={code} message={message}"
                )

            data = payload.get("data")
            if not data:
                break
            if not isinstance(data, list):
                raise RuntimeError(f"Alpha klines payload data is not a list for {alpha_symbol}")
            for item in data:
                row = (
                    int(item[0]),
                    float(item[1]),
                    float(item[2]),
                    float(item[3]),
                    float(item[4]),
                    float(item[5]),
                    int(item[6]),
                    float(item[7]) if item[7] != "" else None,
                    int(item[8]),
                    float(item[9]) if item[9] != "" else None,
                    float(item[10]) if item[10] != "" else None,
                    str(item[11]) if len(item) > 11 else "",
                )
                yield row

            last_open = int(data[-1][0])
            next_start = last_open + 1
            params["startTime"] = next_start
            if end_ts is not None and next_start > end_ts:
                break

    def download_klines_zip(
        self,
        symbol: str,
        interval: str,
        year: int,
        month: int,
        max_retries: int = 6,
        sleep_base_sec: float = 0.5,
    ) -> Iterator[Tuple]:
        """Download monthly zip from data.binance.vision and yield CSV rows.

        Retries with exponential backoff + jitter on transient HTTP/network
        failures. Example URL:
        https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2021-01.zip
        """
        symbol_u = symbol.upper()
        month_str = f"{month:02d}"
        filename = f"{symbol_u}-{interval}-{year}-{month_str}.zip"
        url = f"{self.DATA_ZIP_BASE}/{symbol_u}/{interval}/{filename}"
        attempt = 0
        r = None
        while True:
            try:
                r = self.s.get(url, stream=True, timeout=30)
            except requests.RequestException:
                if attempt >= max_retries:
                    raise
                time.sleep(_retry_after_seconds(None, sleep_base_sec, attempt))
                attempt += 1
                continue
            if _is_transient_status(r.status_code):
                if attempt >= max_retries:
                    r.raise_for_status()
                time.sleep(_retry_after_seconds(r, sleep_base_sec, attempt))
                attempt += 1
                continue
            break
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            # find the CSV file inside
            for name in z.namelist():
                if name.lower().endswith(".csv"):
                    with z.open(name) as fh:
                        text = io.TextIOWrapper(fh, encoding="utf-8")
                        reader = csv.reader(text)
                        for row in reader:
                            open_time = int(row[0])
                            close_time = int(row[6])
                            # Some ZIP datasets may come in microseconds. Normalize to milliseconds.
                            if open_time > 9_999_999_999_999:
                                open_time //= 1000
                            if close_time > 9_999_999_999_999:
                                close_time //= 1000
                            yield (
                                open_time,
                                float(row[1]),
                                float(row[2]),
                                float(row[3]),
                                float(row[4]),
                                float(row[5]),
                                close_time,
                                float(row[7]) if row[7] != "" else None,
                                int(row[8]),
                                float(row[9]) if row[9] != "" else None,
                                float(row[10]) if row[10] != "" else None,
                                row[11] if len(row) > 11 else "",
                            )


if __name__ == "__main__":
    print("binance_hist_downloader: import and use the BinanceDownloader class")
