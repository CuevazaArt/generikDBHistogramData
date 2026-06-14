"""Event definitions for step-by-step backtest logging."""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Event:
    seq: int
    event_time: int | None
    event_type: str
    side: str | None = None
    price: float | None = None
    qty: float | None = None
    cash: float | None = None
    equity: float | None = None
    position_qty: float | None = None
    payload: Dict[str, Any] = field(default_factory=dict)
    trial_id: int | None = None

    def to_record(self) -> Dict[str, Any]:
        return {
            "seq": self.seq,
            "event_time": self.event_time,
            "event_type": self.event_type,
            "side": self.side,
            "price": self.price,
            "qty": self.qty,
            "cash": self.cash,
            "equity": self.equity,
            "position_qty": self.position_qty,
            "payload": self.payload,
            "trial_id": self.trial_id,
        }

