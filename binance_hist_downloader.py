"""Utilities to download Binance klines either via REST API or monthly zip files.

Features:
- `download_klines_api` streams historical klines using Binance REST `/api/v3/klines`.
- `download_klines_zip` downloads monthly zip files from data.binance.vision and yields rows.

Rows are yielded as tuples matching the DB schema in `db.py`.
"""
from typing import Iterator, Optional, Tuple
import requests
import time
import zipfile
import io
import csv


class BinanceDownloader:
    BASE_API = "https://api.binance.com"
    DATA_ZIP_BASE = "https://data.binance.vision/data/spot/monthly/klines"

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
    ) -> Iterator[Tuple]:
        """Yield kline rows (tuples) from Binance REST API.

        Parameters:
        - symbol: e.g. BTCUSDT
        - interval: e.g. 1m, 1h, 1d
        - start_ts, end_ts: milliseconds since epoch
        """
        path = f"/api/v3/klines"
        url = self.BASE_API + path
        params = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ts is not None:
            params["startTime"] = int(start_ts)
        if end_ts is not None:
            params["endTime"] = int(end_ts)

        while True:
            r = self.s.get(url, params=params, timeout=30)
            if r.status_code == 429:
                time.sleep(sleep_on_rate_limit)
                continue
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
            params["startTime"] = last_open + 1
            if end_ts is not None and params["startTime"] > end_ts:
                break

    def download_klines_zip(self, symbol: str, interval: str, year: int, month: int) -> Iterator[Tuple]:
        """Download monthly zip from data.binance.vision and yield rows from the CSV inside.

        Example URL:
        https://data.binance.vision/data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2021-01.zip
        """
        symbol_u = symbol.upper()
        month_str = f"{month:02d}"
        filename = f"{symbol_u}-{interval}-{year}-{month_str}.zip"
        url = f"{self.DATA_ZIP_BASE}/{symbol_u}/{interval}/{filename}"
        r = self.s.get(url, stream=True, timeout=30)
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            # find the CSV file inside
            for name in z.namelist():
                if name.lower().endswith(".csv"):
                    with z.open(name) as fh:
                        text = io.TextIOWrapper(fh, encoding="utf-8")
                        reader = csv.reader(text)
                        for row in reader:
                            yield (
                                int(row[0]),
                                float(row[1]),
                                float(row[2]),
                                float(row[3]),
                                float(row[4]),
                                float(row[5]),
                                int(row[6]),
                                float(row[7]) if row[7] != "" else None,
                                int(row[8]),
                                float(row[9]) if row[9] != "" else None,
                                float(row[10]) if row[10] != "" else None,
                                row[11] if len(row) > 11 else "",
                            )


if __name__ == "__main__":
    print("binance_hist_downloader: import and use the BinanceDownloader class")
