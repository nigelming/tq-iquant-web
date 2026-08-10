from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import Optional

from .event import OrderEvent, TradeEvent
from .account import Account
from .position import Position
from tq_iquant_shared.constants import TradeType, SignalType


class OrderDispatcher(ABC):
    @abstractmethod
    def place_order(self, order: OrderEvent) -> Optional[TradeEvent]:
        ...


class T1Checker(ABC):
    @abstractmethod
    def get_available_shares(self, position: Position, query_date: date) -> int:
        ...


class SimulatedDispatcher(OrderDispatcher):
    def __init__(
        self,
        open_prices: dict = None,
        *,
        min_commission: Decimal = Decimal("5"),
        buy_commission_rate: Decimal = Decimal("0.00025"),
        sell_commission_rate: Decimal = Decimal("0.00025"),
        stamp_duty_rate: Decimal = Decimal("0.0005"),
        slippage: Decimal = Decimal("0"),
    ):
        """成本参数来自组合表，默认值 = 历史硬编码值，老调用不破。"""
        self.open_prices = open_prices or {}
        self.min_commission = min_commission
        self.buy_commission_rate = buy_commission_rate
        self.sell_commission_rate = sell_commission_rate
        self.stamp_duty_rate = stamp_duty_rate
        self.slippage = slippage

    def place_order(self, order: OrderEvent) -> Optional[TradeEvent]:
        price = self.open_prices.get(order.stock_code)
        if not price:
            return None
        # 滑点：买入成交价上浮、卖出成交价下浮
        if order.trade_type.value == "BUY":
            fill_price = price * (Decimal("1") + self.slippage)
        else:
            fill_price = price * (Decimal("1") - self.slippage)
        amount = fill_price * order.quantity
        is_sell = order.trade_type.value == "SELL"
        rate = self.sell_commission_rate if is_sell else self.buy_commission_rate
        commission = max(self.min_commission, amount * rate)
        stamp_duty = amount * self.stamp_duty_rate if is_sell else Decimal("0")
        return TradeEvent(
            strategy_id=order.strategy_id,
            portfolio_id=order.portfolio_id,
            stock_code=order.stock_code,
            trade_type=order.trade_type,
            price=fill_price,
            quantity=order.quantity,
            amount=amount,
            commission=commission,
            stamp_duty=stamp_duty,
            trade_time=order.bar_time,
            signal_type=order.signal_type,
            signal_name=order.signal_name,
        )


class SimulatedT1Checker(T1Checker):
    def get_available_shares(self, position: Position, query_date: date) -> int:
        # T+1：query_date 之前买入的份额可卖，当日买入不可卖
        return position.available_shares_on(query_date)


class LiveT1Checker(T1Checker):
    """实盘 T+1 检查：以桥 /positions 的 available（m_nCanUseVolume）为 SELL 上限。

    LiveEngine 每 bar 刷一次 _available_map（一次 /positions HTTP），
    get_available_shares 取 min(本策略持有量, 桥可用)——Core 账面虚高时不再超发 SELL。
    桥无该仓/未取到 → 全量放行（券商端 T+1 兜底，G6 处理拒单；避免误伤正常卖出）。
    """

    def __init__(self):
        self._available_map = {}

    def set_available_map(self, available_map: dict) -> None:
        self._available_map = available_map or {}

    def get_available_shares(self, position: Position, query_date: date) -> int:
        avail = self._available_map.get(position.stock_code)
        if avail is None:
            return position.quantity
        return min(position.quantity, avail)


class ExecutionEngine:
    def __init__(self, dispatcher: OrderDispatcher, t1_checker: T1Checker):
        self._dispatcher = dispatcher
        self._t1_checker = t1_checker

    def cap_quantity(
        self,
        order: OrderEvent,
        account: Account,
        position: Optional[Position],
    ) -> Optional[int]:
        """审批/T+1 量上限：返回最终下单量（None=不通过不下单）。

        BUY：account.approve_order（策略持仓上限 + 组合现金）；
        SELL：t1_checker 可用股数（回测 Simulated 按日分桶 / 实盘桥 available）。
        execute 与 LiveEngine._handle_bar（切片5 落 submitted 前）共用，保证 DB
        下单量与实发一致。
        """
        if order.trade_type.value == "BUY":
            approved, qty = account.approve_order(
                order.quantity, order.price or Decimal("0"),
                position.market_value if position else Decimal("0"),
            )
            if not approved or qty < 100:
                return None
            return qty
        if position is None or order.bar_time is None:
            return None
        available = self._t1_checker.get_available_shares(
            position, order.bar_time.date()
        )
        qty = min(order.quantity, available)
        if qty < 100:
            return None
        return qty

    def execute(
        self,
        order: OrderEvent,
        account: Account,
        position: Optional[Position],
        apply: bool = True,
    ) -> Optional[TradeEvent]:
        """执行订单。

        apply=True（回测/旧实盘）：place_order 成功后立即 apply_trade 更新账户持仓。
        apply=False（切片5 实盘）：只审批 + 发单，不 apply_trade——成交回报轮询回填
        确认 filled 后才由 LiveEngine._apply_filled_trade 更新（submitted 阶段不 apply，
        避免受理即成交的近似；I4 崩溃窗口下 DB 有 submitted 记录即可）。
        """
        capped = self.cap_quantity(order, account, position)
        if capped is None:
            return None
        order.quantity = capped

        trade = self._dispatcher.place_order(order)
        if not trade:
            return None

        if apply:
            # 统一用 apply_trade 更新账户和持仓
            account.apply_trade(trade)
            if position is not None:
                position.apply_trade(trade)

        return trade

    def reduce_by_ratio(self, position: Position, ratio: Decimal) -> Optional[OrderEvent]:
        if position.quantity == 0:
            return None
        qty = int(position.quantity * ratio / 100) * 100
        if qty < 100:
            return None
        return OrderEvent(
            strategy_id=0,
            portfolio_id=0,
            stock_code=position.stock_code,
            trade_type=TradeType.SELL,
            signal_type=SignalType.REDUCE,
            quantity=qty,
            price=None,
            bar_time=None,
        )
