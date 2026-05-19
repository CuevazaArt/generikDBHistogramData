"""Simple reader for local consumption of klines stored in the sqlite DB.

Other local services can import `reader.query` to fetch data.
"""
from typing import List, Tuple, Optional
from db import query_klines


def query(path: str, symbol: str, interval: str, start_ts: Optional[int] = None, end_ts: Optional[int] = None, limit: Optional[int] = None) -> List[Tuple]:
    return query_klines(path, symbol, interval, start_ts=start_ts, end_ts=end_ts, limit=limit)


if __name__ == "__main__":
    print("reader: import query(path, symbol, interval, ...) to fetch rows")
