from decimal import Decimal
from datetime import date, datetime
from typing import Dict, Optional

from .event import TradeEvent
from tq_iquant_shared.constants import TradeType, SignalType


# 可转债代码前缀（实证自通达信板块32「可转债」331 只，2026-10）：
# 沪市 110/111/113/118（11xxxx.SH，含科创板 118）、
# 深市 123/127/128（12xxxx.SZ）、北交所定向可转债 810（81xxxx.BJ）。
_BOND_PREFIX_SUFFIX = (("11", "SH"), ("12", "SZ"), ("81", "BJ"))


def is_convertible_bond(stock_code: str) -> bool:
    """是否为可转债代码（按交易所+前缀双重判定，避免 11/12/81 数字段误伤其他品种）。"""
    if not stock_code:
        return False
    code, dot, suffix = stock_code.partition(".")
    if not dot or len(code) != 6 or not code.isdigit():
        return False
    return any(code.startswith(pfx) and suffix == sfx
               for pfx, sfx in _BOND_PREFIX_SUFFIX)


def lot_size_for(stock_code: str, bond_lot_enabled: bool) -> int:
    """交易手数（取整单位）：转债开关开启且标的为可转债 → 10 张；其余 100 股（原行为）。"""
    if bond_lot_enabled and is_convertible_bond(stock_code):
        return 10
    return 100


class Position:
    def __init__(
        self,
        stock_code: str,
        *,
        same_day_sellable: bool = False,
        lot_size: int = 100,
    ):
        self.stock_code = stock_code
        self.quantity = 0
        self.avg_cost = Decimal("0")
        self.highest_price = Decimal("0")
        self.buy_time: Optional[datetime] = None
        # 按买入日期分桶的份额，用于 T+1 可卖判断
        self._lots: Dict[date, int] = {}
        # 已加仓次数（首次建仓清零，全平清零），受 max_add_count 约束
        self.add_count = 0
        # 可转债 T+0 回转（可选，默认 False=股票 T+1 原行为）：True 时当日买入可卖
        self.same_day_sellable = same_day_sellable
        # 交易手数（股票 100 股 / 可转债 10 张），供 REDUCE 等按持仓取整场景使用
        self.lot_size = lot_size

    @property
    def market_value(self) -> Decimal:
        return self.avg_cost * self.quantity

    def buy(self, quantity: int, price: Decimal, trade_time: Optional[datetime] = None, is_add: bool = False) -> None:
        # 新开仓（原无持仓）重置加仓计数；加仓（原有持仓）累加
        is_new = self.quantity == 0
        if is_add and self.quantity > 0:
            self.add_count += 1
        elif self.quantity == 0:
            self.add_count = 0
        total_cost = self.avg_cost * self.quantity + price * quantity
        self.quantity += quantity
        self.avg_cost = total_cost / self.quantity
        # 新开仓重置最高价（避免沿用上一笔持仓的旧高点）；加仓时只升不降
        if is_new or price > self.highest_price:
            self.highest_price = price
        if trade_time is not None:
            self._lots[trade_time.date()] = self._lots.get(trade_time.date(), 0) + quantity
            if self.buy_time is None:
                self.buy_time = trade_time

    def update_high(self, price: Decimal) -> None:
        """移动止损用：以最新价刷新持仓期间最高价（只升不降）。"""
        if price > self.highest_price:
            self.highest_price = price

    def sell(self, quantity: int, price: Decimal, trade_time: Optional[datetime] = None) -> tuple:
        if quantity > self.quantity:
            quantity = self.quantity
        sell_amount = price * quantity
        cost_amount = self.avg_cost * quantity
        pnl = sell_amount - cost_amount
        self.quantity -= quantity
        # 全平清零加仓计数
        if self.quantity == 0:
            self.add_count = 0
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
            is_add = trade.signal_type == SignalType.ADD
            self.buy(trade.quantity, trade.price, trade.trade_time, is_add=is_add)
        else:
            self.sell(trade.quantity, trade.price, trade.trade_time)

    def apply_reverse(self, trade: TradeEvent) -> None:
        """反向修正（切片5 G6）：撤回已 apply_trade 的成交（拒单/撤单）。

        买入被撤 → 减持仓（原加了 quantity；均价维持原买入成本不变）
        卖出被撤 → 加持仓（原减了 quantity；回补不加仓，add_count 不变）
        全平被撤回 → 持仓归 0 则重置均价，加仓计数在下次真实 buy 时按逻辑重置。
        仅对已 apply_trade 的部分调用；submitted 阶段未 apply，无需调用。
        """
        if trade.trade_type == TradeType.BUY:
            self.quantity -= trade.quantity
            if self.quantity <= 0:
                self.quantity = 0
                self.avg_cost = Decimal("0")
        else:
            self.quantity += trade.quantity

    def can_sell_on(self, query_date: date) -> bool:
        """T+1：买入当日不可卖，下一交易日及之后可卖。无持仓或当日买入返回 False。
        可转债 T+0（same_day_sellable=True）：有持仓即可卖，含买入当日。"""
        if self.quantity == 0 or self.buy_time is None:
            return False
        if self.same_day_sellable:
            return True
        return query_date > self.buy_time.date()

    def available_shares_on(self, query_date: date) -> int:
        """T+1 可卖股数：query_date 之前（不含当日）买入的份额之和。
        可转债 T+0（same_day_sellable=True）：全部份额可卖（含当日买入）。"""
        if self.same_day_sellable:
            return self.quantity
        return sum(qty for d, qty in self._lots.items() if d < query_date)
