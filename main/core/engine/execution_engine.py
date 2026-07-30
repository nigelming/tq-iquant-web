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
    def __init__(self, open_prices: dict = None):
        self.open_prices = open_prices or {}

    def place_order(self, order: OrderEvent) -> Optional[TradeEvent]:
        price = self.open_prices.get(order.stock_code)
        if not price:
            return None
        return TradeEvent(
            strategy_id=order.strategy_id,
            portfolio_id=order.portfolio_id,
            stock_code=order.stock_code,
            trade_type=order.trade_type,
            price=price,
            quantity=order.quantity,
            amount=price * order.quantity,
            commission=Decimal("5") if price * order.quantity < 20000 else price * order.quantity * Decimal("0.00025"),
            stamp_duty=price * order.quantity * Decimal("0.0005") if order.trade_type.value == "SELL" else Decimal("0"),
            trade_time=order.bar_time,
            signal_type=order.signal_type,
        )


class SimulatedT1Checker(T1Checker):
    def get_available_shares(self, position: Position, query_date: date) -> int:
        # T+1：query_date 之前买入的份额可卖，当日买入不可卖
        return position.available_shares_on(query_date)


class ExecutionEngine:
    def __init__(self, dispatcher: OrderDispatcher, t1_checker: T1Checker):
        self._dispatcher = dispatcher
        self._t1_checker = t1_checker

    def execute(
        self,
        order: OrderEvent,
        account: Account,
        position: Optional[Position],
    ) -> Optional[TradeEvent]:
        if order.trade_type.value == "BUY":
            approved, qty = account.approve_order(
                order.quantity, order.price or Decimal("0"),
                position.market_value if position else Decimal("0"),
            )
            if not approved or qty < 100:
                return None
            order.quantity = qty
        else:
            if position is None or order.bar_time is None:
                return None
            available = self._t1_checker.get_available_shares(
                position, order.bar_time.date()
            )
            qty = min(order.quantity, available)
            if qty < 100:
                return None
            order.quantity = qty

        trade = self._dispatcher.place_order(order)
        if not trade:
            return None

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
