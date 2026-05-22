//! Pure-Rust helpers for the candle pre-pass.
//!
//! These mirror `backtest/transforms.py`:
//! * `apply_heikin_ashi` returns a fresh series with `ha_open/high/low/close`
//!   added on top of the original OHLC fields.
//! * `apply_candle_source` returns the value to be written into the
//!   `price_source` column (in Python this is mutated in place; here we
//!   only return the scalar so the pyo3 wrapper can do the in-place write).

/// One Heikin-Ashi bar (extra columns layered onto an OHLC candle).
#[derive(Debug, Clone, Copy)]
pub struct HeikinAshi {
    pub ha_open: f64,
    pub ha_high: f64,
    pub ha_low: f64,
    pub ha_close: f64,
}

/// Compute the full Heikin-Ashi series from raw OHLC slices.
///
/// Order of operations is byte-equivalent to the Python reference:
/// ```text
/// prev_ha_open  = (open[0] + close[0]) / 2
/// prev_ha_close = (open[0] + high[0] + low[0] + close[0]) / 4
/// for i in 0..n:
///     ha_close = (o[i] + h[i] + l[i] + c[i]) / 4
///     ha_open  = (prev_ha_open + prev_ha_close) / 2  (i > 0)
///     ha_open  = (o[0] + c[0]) / 2                   (i == 0)
///     ha_high  = max(h[i], ha_open, ha_close)
///     ha_low   = min(l[i], ha_open, ha_close)
/// ```
pub fn heikin_ashi(
    opens: &[f64],
    highs: &[f64],
    lows: &[f64],
    closes: &[f64],
) -> Vec<HeikinAshi> {
    let n = opens.len();
    let mut out: Vec<HeikinAshi> = Vec::with_capacity(n);
    if n == 0 {
        return out;
    }
    let mut prev_ha_open = (opens[0] + closes[0]) / 2.0;
    let mut prev_ha_close =
        (opens[0] + highs[0] + lows[0] + closes[0]) / 4.0;
    for i in 0..n {
        let ha_close = (opens[i] + highs[i] + lows[i] + closes[i]) / 4.0;
        let ha_open = if i == 0 {
            (opens[0] + closes[0]) / 2.0
        } else {
            (prev_ha_open + prev_ha_close) / 2.0
        };
        let ha_high = max3(highs[i], ha_open, ha_close);
        let ha_low = min3(lows[i], ha_open, ha_close);
        out.push(HeikinAshi {
            ha_open,
            ha_high,
            ha_low,
            ha_close,
        });
        prev_ha_open = ha_open;
        prev_ha_close = ha_close;
    }
    out
}

#[inline]
fn max3(a: f64, b: f64, c: f64) -> f64 {
    let m1 = if a >= b { a } else { b };
    if m1 >= c {
        m1
    } else {
        c
    }
}

#[inline]
fn min3(a: f64, b: f64, c: f64) -> f64 {
    let m1 = if a <= b { a } else { b };
    if m1 <= c {
        m1
    } else {
        c
    }
}

/// Selects the candle "price source" used by the engine. Mirrors
/// `backtest.transforms.apply_candle_source`.
#[inline]
pub fn candle_source_value(close: f64, ha_close: Option<f64>, source: &str) -> f64 {
    if source == "ha_close" {
        if let Some(v) = ha_close {
            return v;
        }
    }
    close
}
