from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Dict, Optional

from tq_iquant_shared.constants import SignalType, TradeType


@dataclass
class BarEvent:
    stocks: Dict[str, Dict[str, object]] = field(default_factory=dict)
    bar_time: Optional[datetime] = None
    # C6：驱动 bar 所属周期（"1m"/"5m"/"1d"...）。None=未标注（回测/旧调用，处理全部策略）。
    period: Optional[str] = None
    # BarPoller 本轮该 bar 各 code 拉到的原始 bar dict 列表（count 窗口，含 forming 最新一根），
    # 透传给注入复用——消除 1m 双拉：BarPoller 已拉判完成，注入并入预热缓存直接复用不再重拉。
    # None=非 BarPoller 路径（回测/手动构造），注入走 _get_bars_with_increment 增量拉。
    bars_by_code: Optional[Dict[str, list]] = None


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
    # 触发该订单的信号名（公式变量名如 open_sig，风控如 stop_loss/take_profit/trailing_stop）
    signal_name: str = ""


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
    signal_type: Optional[SignalType] = None
    # 触发该成交的信号名，由 OrderEvent 透传（供交易明细定位来源）
    signal_name: str = ""
