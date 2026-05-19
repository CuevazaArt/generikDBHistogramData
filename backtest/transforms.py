"""Feature transforms for market candles."""
from typing import Dict, List


def apply_heikin_ashi(candles: List[Dict]) -> List[Dict]:
    if not candles:
        return []
    out: List[Dict] = []
    prev_ha_open = (candles[0]["open"] + candles[0]["close"]) / 2.0
    prev_ha_close = (candles[0]["open"] + candles[0]["high"] + candles[0]["low"] + candles[0]["close"]) / 4.0
    for i, c in enumerate(candles):
        ha_close = (c["open"] + c["high"] + c["low"] + c["close"]) / 4.0
        if i == 0:
            ha_open = (c["open"] + c["close"]) / 2.0
        else:
            ha_open = (prev_ha_open + prev_ha_close) / 2.0
        ha_high = max(c["high"], ha_open, ha_close)
        ha_low = min(c["low"], ha_open, ha_close)
        nc = dict(c)
        nc["ha_open"] = ha_open
        nc["ha_high"] = ha_high
        nc["ha_low"] = ha_low
        nc["ha_close"] = ha_close
        out.append(nc)
        prev_ha_open = ha_open
        prev_ha_close = ha_close
    return out


def apply_candle_source(candles: List[Dict], source: str = "close") -> List[Dict]:
    for c in candles:
        if source == "ha_close" and "ha_close" in c:
            c["price_source"] = float(c["ha_close"])
        else:
            c["price_source"] = float(c["close"])
    return candles

