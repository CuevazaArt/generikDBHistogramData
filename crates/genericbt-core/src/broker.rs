//! Pure-Rust port of `backtest.broker.SpotBroker` (1:1, no pyo3).
//!
//! Numerical behaviour mirrors [backtest/broker.py] line-for-line, including
//! the `max(0.0, min(1.0, size_pct))` clamp form (rather than `f64::clamp`,
//! which has different NaN semantics) and the `1e-12` zero-out tolerance
//! on `position_qty` after sells.

/// In-memory broker state, identical layout to Python `BrokerState`.
#[derive(Debug, Clone)]
pub struct BrokerState {
    pub cash: f64,
    pub position_qty: f64,
    pub avg_entry: f64,
}

impl BrokerState {
    pub fn new(initial_cash: f64) -> Self {
        Self {
            cash: initial_cash,
            position_qty: 0.0,
            avg_entry: 0.0,
        }
    }
}

/// Concrete fill emitted by `execute_market`. Matches the keys returned by
/// the Python broker dict: `{"side", "price", "qty", "fee"}`.
#[derive(Debug, Clone)]
pub struct Fill {
    pub side: &'static str,
    pub price: f64,
    pub qty: f64,
    pub fee: f64,
}

/// Pure-Rust mirror of `backtest.broker.SpotBroker`.
#[derive(Debug, Clone)]
pub struct SpotBroker {
    pub state: BrokerState,
    pub fee_rate: f64,
    pub slippage_bps: f64,
}

impl SpotBroker {
    pub fn new(initial_cash: f64, fee_rate: f64, slippage_bps: f64) -> Self {
        Self {
            state: BrokerState::new(initial_cash),
            fee_rate,
            slippage_bps,
        }
    }

    /// Equivalent to Python `mark_equity`: cash + position * mark_price.
    #[inline]
    pub fn mark_equity(&self, mark_price: f64) -> f64 {
        self.state.cash + self.state.position_qty * mark_price
    }

    #[inline]
    fn slipped_price(&self, price: f64, side: &str) -> f64 {
        let slip = (self.slippage_bps / 10_000.0) * price;
        if side == "buy" {
            price + slip
        } else {
            price - slip
        }
    }

    /// Python-equivalent clamp: `max(0.0, min(1.0, x))`.
    ///
    /// Uses explicit branches rather than `f64::clamp` so that the same
    /// ordering and tie-breaking semantics apply on platforms where
    /// `f64::clamp` would otherwise panic on NaN.
    #[inline]
    fn clamp01(x: f64) -> f64 {
        let upper = if x < 1.0 { x } else { 1.0 };
        if upper > 0.0 {
            upper
        } else {
            0.0
        }
    }

    /// 1:1 port of `SpotBroker.execute_market`. Returns `None` for invalid
    /// side, zero notional, or sell when flat.
    pub fn execute_market(&mut self, side: &str, price: f64, size_pct: f64) -> Option<Fill> {
        let side_lc = side.to_ascii_lowercase();
        if side_lc != "buy" && side_lc != "sell" {
            return None;
        }
        let exec_price = self.slipped_price(price, &side_lc);
        let pct = Self::clamp01(size_pct);

        if side_lc == "buy" {
            let notional = self.state.cash * pct;
            if notional <= 0.0 {
                return None;
            }
            let fee = notional * self.fee_rate;
            let qty = (notional - fee) / exec_price;
            if qty <= 0.0 {
                return None;
            }
            let old_cost = self.state.avg_entry * self.state.position_qty;
            self.state.position_qty += qty;
            self.state.cash -= notional;
            if self.state.position_qty > 0.0 {
                self.state.avg_entry =
                    (old_cost + qty * exec_price) / self.state.position_qty;
            }
            return Some(Fill {
                side: "buy",
                price: exec_price,
                qty,
                fee,
            });
        }

        // sell branch
        if self.state.position_qty <= 0.0 {
            return None;
        }
        let qty = self.state.position_qty * pct;
        let gross = qty * exec_price;
        let fee = gross * self.fee_rate;
        self.state.position_qty -= qty;
        self.state.cash += gross - fee;
        if self.state.position_qty <= 1e-12 {
            self.state.position_qty = 0.0;
            self.state.avg_entry = 0.0;
        }
        Some(Fill {
            side: "sell",
            price: exec_price,
            qty,
            fee,
        })
    }
}
