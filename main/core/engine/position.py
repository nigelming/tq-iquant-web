from decimal import Decimal
from datetime import date, datetime
from typing import Dict, Optional

from .event import TradeEvent
from tq_iquant_shared.constants import TradeType


class Position:
    def __init__(self, stock_code: str):
        self.stock_code = stock_code
        self.quantity = 0
        self.avg_cost = Decimal("0")
        self.highest_price = Decimal("0")
        self.buy_time: Optional[datetime] = None
        # 按买入日期分桶的份额，用于 T+1 可卖判断
        self._lots: Dict[date, int] = {}

    @property
    def market_value(self) -> Decimal:
        return self.avg_cost * self.quantity

    def buy(self, quantity: int, price: Decimal, trade_time: Optional[datetime] = None) -> None:
        total_cost = self.avg_cost * self.quantity + price * quantity
        self.quantity += quantity
        self.avg_cost = total_cost / self.quantity
        if price > self.highest_price:
            self.highest_price = price
        if trade_time is not None:
            self._lots[trade_time.date()] = self._lots.get(trade_time.date(), 0) + quantity
            if self.buy_time is None:
                self.buy_time = trade_time

    def sell(self, quantity: int, price: Decimal, trade_time: Optional[datetime] = None) -> tuple:
        if quantity > self.quantity:
            quantity = self.quantity
        sell_amount = price * quantity
        cost_amount = self.avg_cost * quantity
        pnl = sell_amount - cost_amount
        self.quantity -= quantity
        # 按先进先出从最早的可卖桶扣减
        if trade_time is not None:
            remaining = quantity
            for d in sorted(self._lots.keys()):
                if remaining <= 0:
                    break
                take = min(remaining, self._lots[d])
                self._lots[d] -= take
                remaining -= take
            # 清理空桶
            self._lots = {d: q for d, q in self._lots.items() if q > 0}
        return pnl, sell_amount

    def apply_trade(self, trade: TradeEvent) -> None:
        """成交后统一更新持仓（买入加权均价+最高价，卖出减仓）。"""
        if trade.trade_type == TradeType.BUY:
            self.buy(trade.quantity, trade.price, trade.trade_time)
        else:
            self.sell(trade.quantity, trade.price, trade.trade_time)

    def can_sell_on(self, query_date: date) -> bool:
        """T+1：买入当日不可卖，下一交易日及之后可卖。无持仓或当日买入返回 False。"""
        if self.quantity == 0 or self.buy_time is None:
            return False
        return query_date > self.buy_time.date()

    def available_shares_on(self, query_date: date) -> int:
        """T+1 可卖股数：query_date 之前（不含当日）买入的份额之和。"""
        return sum(qty for d, qty in self._lots.items() if d < query_date)
