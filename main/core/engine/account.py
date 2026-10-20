from decimal import Decimal
import logging
from typing import Optional

from .event import TradeEvent
from tq_iquant_shared.constants import TradeType

logger = logging.getLogger(__name__)


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
        # 费用预留（可选，默认 None=不启用/原行为）：(最低佣金, 买入佣金费率)。
        # 启用后 approve_order 按「价×量 + 佣金」口径审批，避免 apply_trade
        # 扣佣金后现金为负。仅买入方向需要预留（卖出费用从卖出款内扣，不会负）。
        self.buy_fee_cfg: Optional[tuple] = None

    def _buy_needed(self, quantity: int, price: Decimal) -> Decimal:
        """买入所需资金：默认 价×量；启用费用预留时含佣金 max(最低佣金, 金额×费率)。"""
        amount = price * quantity
        if self.buy_fee_cfg is None:
            return amount
        min_commission, buy_rate = self.buy_fee_cfg
        return amount + max(min_commission, amount * buy_rate)

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
        strategy_capital_limit_override: Optional[Decimal] = None,
        lot_size: int = 100,
    ) -> tuple:
        """双层卡控：策略持仓上限 + 组合现金。返回 (approved, qty)。

        needed = price * quantity
        策略上限可用 = strategy_capital_limit - position_value（若设了上限）
        组合现金可用 = cash
        可买市值 = min(策略上限可用, 组合现金)
        按可买市值缩减到 1 手整数倍（股票 100 股 / 可转债 10 张）；不足 1 手则拒绝并计数。

        strategy_capital_limit_override（#3 策略级敞口上限，可选）：非 None 时
        优先于 self.strategy_capital_limit 生效——Account 是组合级单例，策略级
        上限由调用方按单传入（旧调用不传 → 行为不变）。

        lot_size（可选，默认 100=原行为）：可转债 10 张取整时由调用方传入 10。
        """
        if price <= 0:
            # 非正价格（前复权伪影/脏数据）：needed=price*qty<=cap 对负价恒成立，
            # 直接拒绝，防止负价买入现金倒增 + 巨量股数失真。
            self.insufficient_count += 1
            return False, 0
        needed = self._buy_needed(quantity, price)
        # 确认可用资金上限：取策略上限与组合现金的较小值
        cap = self.cash
        limit = (strategy_capital_limit_override
                 if strategy_capital_limit_override is not None
                 else self.strategy_capital_limit)
        if limit is not None:
            strategy_available = limit - position_value
            if strategy_available < cap:
                cap = strategy_available
        if needed <= cap:
            return True, quantity
        # 缩减到整手；启用费用预留时含佣口径仍超 cap 的逐手递减
        # （佣金差额相对金额极小，通常 0~2 手即收敛）
        max_qty = int(cap / price / lot_size) * lot_size
        while max_qty >= lot_size and self._buy_needed(max_qty, price) > cap:
            max_qty -= lot_size
        if max_qty >= lot_size:
            return True, max_qty
        logger.debug(
            "资金拦截:不足1手拒绝（需 %s，可用 %s @ 价 %s，缩减后 %d 股）",
            needed, cap, price, max_qty,
        )
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
