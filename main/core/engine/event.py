from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, Optional

from tq_iquant_shared.constants import SignalType, TradeType


@dataclass
class BarEvent:
    stocks: Dict[str, Dict[str, object]] = field(default_factory=dict)
    bar_time: Optional[datetime] = None


@dataclass
class SignalEvent:
    strategy_id: int
    stock_code: str
    signal_name: str
    signal_type: SignalType
    bar_time: datetime


@dataclass
class RiskEvent:
    strategy_id: Optional[int] = None
    rule: str = ""
    stock_code: Optional[str] = None
    bar_time: Optional[datetime] = None


@dataclass
class OrderEvent:
    strategy_id: int
    portfolio_id: int
    stock_code: str
    trade_type: TradeType
    signal_type: SignalType
    quantity: int
    price: Optional[Decimal] = None
    bar_time: Optional[datetime] = None


@dataclass
class TradeEvent:
    strategy_id: int
    portfolio_id: int
    stock_code: str
    trade_type: TradeType
    price: Decimal
    quantity: int
    amount: Decimal
    commission: Decimal
    stamp_duty: Decimal
    trade_time: datetime
