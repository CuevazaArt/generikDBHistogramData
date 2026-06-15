# earn_rates.py
"""Fetch Binance Earn fixed‑rate loan APRs.

Provides functions to retrieve loan interest rates and collateral combinations
from the Binance SAPI endpoints.

The module uses Binance **SAPI** endpoints that require an API key + signature.
Credentials must be supplied via environment variables or a ``.env`` file.
"""

import os
import time
from typing import List, Dict
import requests
import hmac
import hashlib
import urllib.parse

# ---------------------------------------------------------------------------
# Environment & auth helpers
# ---------------------------------------------------------------------------

def _load_env_file():
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if "=" in line:
                key, val = line.split('=', 1)
                os.environ.setdefault(key.strip(), val.strip())

_load_env_file()

BASE_URL = "https://api.binance.com"


def _get_server_time() -> int:
    """Fetch Binance server timestamp to avoid clock‑skew rejections."""
    resp = requests.get(f"{BASE_URL}/api/v3/time", timeout=10)
    resp.raise_for_status()
    return resp.json()["serverTime"]


def _api_key() -> str:
    key = os.getenv("BINANCE_API_KEY") or os.getenv("API_KEY")
    if not key:
        raise RuntimeError("BINANCE_API_KEY environment variable not set")
    return key


def _api_secret() -> str:
    secret = os.getenv("BINANCE_API_SECRET") or os.getenv("API_SECRET")
    if not secret:
        raise RuntimeError("BINANCE_API_SECRET environment variable not set")
    return secret


def _signed_get(endpoint: str, extra_params: dict | None = None) -> dict:
    """Perform a signed GET request against a Binance SAPI endpoint.

    Steps:
      1. Fetch server time to avoid ``-1021 Timestamp`` errors.
      2. Build query string with ``timestamp`` + ``recvWindow``.
      3. HMAC‑SHA256 sign the **raw** query string (no percent‑encoding).
      4. Append signature and send.
    """
    server_time = _get_server_time()
    params: dict = {"timestamp": server_time, "recvWindow": 5000}
    if extra_params:
        params.update(extra_params)
    query = urllib.parse.urlencode(params)
    sig = hmac.new(
        _api_secret().encode(), query.encode(), hashlib.sha256
    ).hexdigest()
    url = f"{BASE_URL}{endpoint}?{query}&signature={sig}"
    headers = {"X-MBX-APIKEY": _api_key()}

    attempt = 0
    while True:
        try:
            resp = requests.get(url, headers=headers, timeout=15)
        except requests.RequestException:
            if attempt >= 4:
                raise
            time.sleep(0.5 * (2 ** attempt))
            attempt += 1
            continue
        if resp.status_code == 429:
            retry = int(resp.headers.get("Retry-After", "1"))
            time.sleep(retry)
            attempt += 1
            if attempt > 4:
                resp.raise_for_status()
            continue
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_flexible_loan_rates() -> List[Dict]:
    """Retrieve all flexible‑loan interest rates from Binance.

    Calls ``GET /sapi/v2/loan/flexible/loanable/data`` (signed, USER_DATA).

    Returns a list of dicts::

        {"asset": "USDT", "rate": 0.045}  # rate as decimal (4.5 %)
    """
    payload = _signed_get("/sapi/v2/loan/flexible/loanable/data")
    results = []
    for row in payload.get("rows", []):
        asset = row.get("loanCoin")
        try:
            rate = float(row.get("flexibleInterestRate", 0)) / 100.0
        except (ValueError, TypeError):
            rate = 0.0
        if asset:
            results.append({"asset": asset, "rate": rate})
    return results


def get_collateral_assets() -> List[Dict]:
    """Retrieve all collateral assets from Binance.

    Calls ``GET /sapi/v2/loan/flexible/collateral/data`` (signed, USER_DATA).

    Returns a list of dicts with collateral coin and LTV info.
    """
    payload = _signed_get("/sapi/v2/loan/flexible/collateral/data")
    results = []
    for row in payload.get("rows", []):
        coin = row.get("collateralCoin")
        if coin:
            results.append({
                "collateral": coin,
                "initial_ltv": float(row.get("initialLTV", 0)),
                "margin_call_ltv": float(row.get("marginCallLTV", 0)),
                "liquidation_ltv": float(row.get("liquidationLTV", 0)),
            })
    return results


def get_collateral_loan_combinations(
    loan_rates: List[Dict] | None = None,
    collaterals: List[Dict] | None = None,
    max_rate: float = 0.03,
) -> List[Dict]:
    """Build a representative list of loanable‑collateral pairs.

    Accepts pre‑fetched data to avoid redundant API calls.  Only pairs whose
    annual rate is ≤ ``max_rate`` are returned.
    """
    if loan_rates is None:
        loan_rates = get_flexible_loan_rates()
    if collaterals is None:
        collaterals = get_collateral_assets()
    collateral_coins = {c["collateral"] for c in collaterals}

    combinations = []
    for lr in loan_rates:
        if lr["rate"] > max_rate:
            continue
        for col in collateral_coins:
            if col == lr["asset"]:
                continue
            combinations.append({
                "loan_asset": lr["asset"],
                "collateral_asset": col,
                "rate": lr["rate"],
            })
    combinations.sort(key=lambda x: (x["rate"], x["loan_asset"]))
    return combinations


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_table(rows, headers):
    """Pretty‑print a list of row‑lists under the given headers."""
    if not rows:
        return
    col_widths = [
        max(len(str(item)) for item in [hdr] + [row[i] for row in rows])
        for i, hdr in enumerate(headers)
    ]
    header_line = " | ".join(
        str(hdr).ljust(col_widths[i]) for i, hdr in enumerate(headers)
    )
    separator = "-+-".join("-" * w for w in col_widths)
    print(header_line)
    print(separator)
    for row in rows:
        print(" | ".join(str(row[i]).ljust(col_widths[i]) for i in range(len(headers))))


if __name__ == "__main__":
    MAX_RATE = 0.03  # <= 3 %

    # ── Fetch data once ──
    print("Consultando datos de Binance Earn...")
    all_rates = get_flexible_loan_rates()
    all_collaterals = get_collateral_assets()

    # ── Tasas de préstamo flexibles ──
    filtered = [r for r in all_rates if r["rate"] <= MAX_RATE]
    filtered.sort(key=lambda x: x["rate"])

    if filtered:
        print(f"\nActivos con tasa anual <= {MAX_RATE:.0%} ({len(filtered)} de {len(all_rates)} totales):")
        _print_table(
            [[r["asset"], f"{r['rate']:.4%}"] for r in filtered],
            ["Activo", "Tasa APR"],
        )
    else:
        print(f"\nNo se encontraron activos con tasa <= {MAX_RATE:.0%}.")

    # ── Combinaciones colateralizadas ──
    combos = get_collateral_loan_combinations(
        loan_rates=all_rates, collaterals=all_collaterals, max_rate=MAX_RATE
    )

    if combos:
        # Show a summary: group by loan_asset
        seen_loans = {}
        for c in combos:
            if c["loan_asset"] not in seen_loans:
                seen_loans[c["loan_asset"]] = c
        summary = list(seen_loans.values())

        print(f"\nResumen — un colateral representativo por activo ({len(summary)} activos, {len(combos)} combinaciones totales):")
        _print_table(
            [[s["loan_asset"], s["collateral_asset"], f"{s['rate']:.4%}"] for s in summary],
            ["Activo Préstamo", "Colateral (ejemplo)", "Tasa APR"],
        )
    else:
        print(f"\nNo se encontraron combinaciones con tasa <= {MAX_RATE:.0%}.")
