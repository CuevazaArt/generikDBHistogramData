//! Technical indicators ported 1:1 from `backtest/indicators.py`.
//!
//! Returns `Vec<Option<f64>>` to preserve the Python "None during warmup"
//! semantics; the Rust engine then forwards `None` as Python `None` so that
//! consumer strategies see the same signature whether the engine is Python
//! or Rust.

/// Rolling simple moving average. Returns `None` for indices `< period - 1`.
///
/// Matches `backtest.indicators._rolling_sma`: a sliding window sum,
/// subtracting the value that leaves the window once `i >= period`.
pub fn rolling_sma(values: &[f64], period: usize) -> Vec<Option<f64>> {
    let n = values.len();
    let mut out: Vec<Option<f64>> = vec![None; n];
    if period == 0 {
        return out;
    }
    let p = period as f64;
    let mut s: f64 = 0.0;
    for (i, &v) in values.iter().enumerate() {
        s += v;
        if i >= period {
            s -= values[i - period];
        }
        if i + 1 >= period {
            out[i] = Some(s / p);
        }
    }
    out
}

/// Wilder-style EMA (not classic; see python source). Returns `None` until
/// `i >= period - 1`, then emits the EMA computed from the start of the
/// series with `alpha = 2 / (period + 1)`.
pub fn rolling_ema(values: &[f64], period: usize) -> Vec<Option<f64>> {
    let n = values.len();
    let mut out: Vec<Option<f64>> = vec![None; n];
    if period == 0 || n == 0 {
        return out;
    }
    let alpha = 2.0 / (period as f64 + 1.0);
    let one_minus_alpha = 1.0 - alpha;
    let mut ema: Option<f64> = None;
    for (i, &v) in values.iter().enumerate() {
        ema = Some(match ema {
            None => v,
            Some(e) => alpha * v + one_minus_alpha * e,
        });
        if i + 1 >= period {
            out[i] = ema;
        }
    }
    out
}

/// Wilder smoothing RSI. Returns `None` until `i >= period`, then a value in
/// `[0, 100]`. Identical formula to `backtest.indicators._rsi`.
pub fn rsi(values: &[f64], period: usize) -> Vec<Option<f64>> {
    let n = values.len();
    let mut out: Vec<Option<f64>> = vec![None; n];
    if period == 0 || n <= period {
        return out;
    }
    let mut gains: Vec<f64> = Vec::with_capacity(n - 1);
    let mut losses: Vec<f64> = Vec::with_capacity(n - 1);
    for i in 1..n {
        let diff = values[i] - values[i - 1];
        gains.push(if diff > 0.0 { diff } else { 0.0 });
        losses.push(if diff < 0.0 { -diff } else { 0.0 });
    }
    let p_f = period as f64;
    let mut avg_gain: f64 = gains[..period].iter().sum::<f64>() / p_f;
    let mut avg_loss: f64 = losses[..period].iter().sum::<f64>() / p_f;

    out[period] = Some(if avg_loss == 0.0 {
        100.0
    } else {
        let rs = avg_gain / avg_loss;
        100.0 - (100.0 / (1.0 + rs))
    });

    for i in (period + 1)..n {
        let gain = gains[i - 1];
        let loss = losses[i - 1];
        avg_gain = ((avg_gain * (p_f - 1.0)) + gain) / p_f;
        avg_loss = ((avg_loss * (p_f - 1.0)) + loss) / p_f;
        out[i] = Some(if avg_loss == 0.0 {
            100.0
        } else {
            let rs = avg_gain / avg_loss;
            100.0 - (100.0 / (1.0 + rs))
        });
    }
    out
}

/// Average True Range backed by `rolling_ema`. Identical to
/// `backtest.indicators._atr`: TR[0] = 0; TR[i] = max(h-l, |h-pc|, |l-pc|).
pub fn atr(
    highs: &[f64],
    lows: &[f64],
    closes: &[f64],
    period: usize,
) -> Vec<Option<f64>> {
    let n = highs.len();
    let mut out: Vec<Option<f64>> = vec![None; n];
    if period == 0 || n < 2 {
        return out;
    }
    let mut trs: Vec<f64> = Vec::with_capacity(n);
    trs.push(0.0);
    for i in 1..n {
        let a = highs[i] - lows[i];
        let b = (highs[i] - closes[i - 1]).abs();
        let c = (lows[i] - closes[i - 1]).abs();
        // Python's max(a, b, c) — note: NaN propagation only matters for
        // pathological inputs; price feeds never include NaN.
        let m1 = if a >= b { a } else { b };
        let tr = if m1 >= c { m1 } else { c };
        trs.push(tr);
    }
    let _ = out;
    rolling_ema(&trs, period)
}

/// Three-decimal-friendly form of `max(highs[i] - lows[i], ...)` reused by
/// the engine's pre-pass. Currently only used by tests.
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sma_basic() {
        let v = vec![1.0, 2.0, 3.0, 4.0, 5.0];
        let s = rolling_sma(&v, 3);
        assert_eq!(s[0], None);
        assert_eq!(s[1], None);
        assert!((s[2].unwrap() - 2.0).abs() < 1e-12);
        assert!((s[3].unwrap() - 3.0).abs() < 1e-12);
        assert!((s[4].unwrap() - 4.0).abs() < 1e-12);
    }

    #[test]
    fn ema_period_one_passthrough() {
        let v = vec![10.0, 20.0, 30.0];
        let e = rolling_ema(&v, 1);
        assert_eq!(e[0], Some(10.0));
        assert_eq!(e[1], Some(20.0));
        assert_eq!(e[2], Some(30.0));
    }

    #[test]
    fn rsi_short_series_all_none() {
        let v = vec![1.0, 2.0, 3.0];
        let r = rsi(&v, 14);
        assert!(r.iter().all(|x| x.is_none()));
    }
}
