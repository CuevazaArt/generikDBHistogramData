"""Simple spot broker simulator with fee and slippage."""
from dataclasses import dataclass
from typing import Dict, Optional


@dataclass
class BrokerState:
    cash: float
    position_qty: float
    avg_entry: float


class SpotBroker:
    def __init__(self, initial_cash: float, fee_rate: float = 0.001, slippage_bps: float = 2.0):
        self.state = BrokerState(cash=float(initial_cash), position_qty=0.0, avg_entry=0.0)
        self.fee_rate = float(fee_rate)
        self.slippage_bps = float(slippage_bps)

    def mark_equity(self, mark_price: float) -> float:
        return self.state.cash + (self.state.position_qty * float(mark_price))

    def _slipped_price(self, price: float, side: str) -> float:
        slip = (self.slippage_bps / 10_000.0) * float(price)
        if side == "buy":
            return float(price) + slip
        return float(price) - slip

    def execute_market(self, side: str, price: float, size_pct: float = 1.0) -> Optional[Dict]:
        side = side.lower()
        if side not in ("buy", "sell"):
            return None
        exec_price = self._slipped_price(float(price), side)
        if side == "buy":
            notional = self.state.cash * max(0.0, min(1.0, float(size_pct)))
            if notional <= 0:
                return None
            fee = notional * self.fee_rate
            qty = (notional - fee) / exec_price
            if qty <= 0:
                return None
            old_cost = self.state.avg_entry * self.state.position_qty
            self.state.position_qty += qty
            self.state.cash -= notional
            if self.state.position_qty > 0:
                self.state.avg_entry = (old_cost + qty * exec_price) / self.state.position_qty
            return {"side": "buy", "price": exec_price, "qty": qty, "fee": fee}
        if self.state.position_qty <= 0:
            return None
        qty = self.state.position_qty * max(0.0, min(1.0, float(size_pct)))
        gross = qty * exec_price
        fee = gross * self.fee_rate
        self.state.position_qty -= qty
        self.state.cash += gross - fee
        if self.state.position_qty <= 1e-12:
            self.state.position_qty = 0.0
            self.state.avg_entry = 0.0
        return {"side": "sell", "price": exec_price, "qty": qty, "fee": fee}

