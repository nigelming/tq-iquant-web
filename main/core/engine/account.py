from decimal import Decimal
from typing import Optional

from .event import TradeEvent
from tq_iquant_shared.constants import TradeType


class Account:
    def __init__(
        self,
        initial_capital: Decimal,
        strategy_capital_limit: Optional[Decimal] = None,
    ):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.insufficient_count = 0
        # 策略资金占比对应的上限（持仓市值不可超过此值）。None 表示不卡策略层。
        self.strategy_capital_limit = strategy_capital_limit

    @property
    def market_value(self) -> Decimal:
        return Decimal("0")

    @property
    def total_value(self) -> Decimal:
        return self.cash

    def approve_order(
        self,
        quantity: int,
        price: Decimal,
        position_value: Decimal,
    ) -> tuple:
        """双层卡控：策略持仓上限 + 组合现金。返回 (approved, qty)。

        needed = price * quantity
        策略上限可用 = strategy_capital_limit - position_value（若设了上限）
        组合现金可用 = cash
        可买市值 = min(策略上限可用, 组合现金)
        按可买市值缩减到 100 股整数倍；不足 1 手则拒绝并计数。
        """
        needed = price * quantity
        # 确认可用资金上限：取策略上限与组合现金的较小值
        cap = self.cash
        if self.strategy_capital_limit is not None:
            strategy_available = self.strategy_capital_limit - position_value
            if strategy_available < cap:
                cap = strategy_available
        if needed <= cap:
            return True, quantity
        # 缩减到 100 股整数倍
        max_qty = int(cap / price / 100) * 100
        if max_qty >= 100:
            return True, max_qty
        self.insufficient_count += 1
        return False, 0

    def deduct_cash(self, amount: Decimal) -> None:
        self.cash -= amount

    def add_cash(self, amount: Decimal) -> None:
        self.cash += amount

    def apply_trade(self, trade: TradeEvent) -> None:
        """成交后更新现金：买扣（金额+佣金+印花税），卖加（金额-佣金-印花税）。"""
        if trade.trade_type == TradeType.BUY:
            self.deduct_cash(trade.amount + trade.commission + trade.stamp_duty)
        else:
            self.add_cash(trade.amount - trade.commission - trade.stamp_duty)

    def apply_reverse(self, trade: TradeEvent) -> None:
        """反向修正（切片5 G6）：撤回已 apply_trade 的成交（拒单/撤单）。

        买入被撤 → 现金加回（原扣了 amount+佣金+印花税）
        卖出被撤 → 现金扣回（原加了 amount-佣金-印花税）
        仅在已 apply_trade 且后来确认未成/撤单时调用；submitted 阶段未 apply，无需调用。
        """
        if trade.trade_type == TradeType.BUY:
            self.add_cash(trade.amount + trade.commission + trade.stamp_duty)
        else:
            self.deduct_cash(trade.amount - trade.commission - trade.stamp_duty)
