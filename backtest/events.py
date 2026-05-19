"""Event definitions for step-by-step backtest logging."""
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Event:
    seq: int
    event_time: Optional[int]
    event_type: str
    side: Optional[str] = None
    price: Optional[float] = None
    qty: Optional[float] = None
    cash: Optional[float] = None
    equity: Optional[float] = None
    position_qty: Optional[float] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    trial_id: Optional[int] = None

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

