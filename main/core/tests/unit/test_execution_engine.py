from decimal import Decimal
from datetime import datetime

from core.engine.execution_engine import (
    ExecutionEngine, SimulatedDispatcher, SimulatedT1Checker,
)
from core.engine.account import Account
from core.engine.position import Position
from core.engine.event import OrderEvent
from tq_iquant_shared.constants import TradeType, SignalType


def _order(trade_type, stock_code, quantity, price, bar_time=None):
    return OrderEvent(
        strategy_id=1,
        portfolio_id=1,
        stock_code=stock_code,
        trade_type=trade_type,
        signal_type=SignalType.OPEN,
        quantity=quantity,
        price=Decimal(price),
        bar_time=bar_time or datetime(2026, 7, 30, 15, 0),
    )


def test_execute_buy_approves_and_updates_account_position():
    acc = Account(Decimal("100000"), strategy_capital_limit=Decimal("60000"))
    pos = Position("000001.SZ")
    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10.5")})
    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())

    order = _order(TradeType.BUY, "000001.SZ", 1000, "10.5",
                   bar_time=datetime(2026, 7, 29, 15, 0))
    trade = engine.execute(order, acc, pos)

    assert trade is not None
    assert trade.quantity == 1000
    assert trade.price == Decimal("10.5")
    # Account 扣款
    assert acc.cash == Decimal("100000") - (trade.amount + trade.commission + trade.stamp_duty)
    # Position 更新
    assert pos.quantity == 1000
    assert pos.avg_cost == Decimal("10.5")
    assert pos.buy_time == datetime(2026, 7, 29, 15, 0)


def test_execute_buy_exceeds_cash_reduces_quantity():
    acc = Account(Decimal("5000"), strategy_capital_limit=Decimal("100000"))
    pos = Position("000001.SZ")
    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})
    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())

    # 想买 1000 股 × 10 = 10000，现金 5000 → 缩到 500 股
    order = _order(TradeType.BUY, "000001.SZ", 1000, "10")
    trade = engine.execute(order, acc, pos)

    assert trade is not None
    assert trade.quantity == 500
    assert pos.quantity == 500


def test_execute_buy_below_one_lot_rejected():
    acc = Account(Decimal("400"), strategy_capital_limit=Decimal("100000"))
    pos = Position("000001.SZ")
    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})
    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())

    order = _order(TradeType.BUY, "000001.SZ", 1000, "10")
    trade = engine.execute(order, acc, pos)

    assert trade is None
    assert pos.quantity == 0
    assert acc.insufficient_count == 1


def test_execute_sell_t1_same_day_blocked():
    acc = Account(Decimal("100000"))
    pos = Position("000001.SZ")
    # 当天买入
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 30, 9, 30)))
    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("11")})
    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())

    # 同一交易日尝试卖出 → T+1 阻止
    order = _order(TradeType.SELL, "000001.SZ", 1000, "11",
                   bar_time=datetime(2026, 7, 30, 15, 0))
    trade = engine.execute(order, acc, pos)

    assert trade is None
    assert pos.quantity == 1000  # 未卖出


def test_execute_sell_next_day_succeeds():
    acc = Account(Decimal("100000"))
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("11")})
    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())

    order = _order(TradeType.SELL, "000001.SZ", 1000, "11",
                   bar_time=datetime(2026, 7, 30, 15, 0))
    trade = engine.execute(order, acc, pos)

    assert trade is not None
    assert trade.quantity == 1000
    assert pos.quantity == 0
    # 卖出回款
    assert acc.cash == Decimal("100000") + (trade.amount - trade.commission - trade.stamp_duty)


def _buy(price, quantity, trade_time):
    from core.engine.event import TradeEvent
    return TradeEvent(
        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",
        trade_type=TradeType.BUY, price=Decimal(price), quantity=quantity,
        amount=Decimal(price) * quantity, commission=Decimal("0"),
        stamp_duty=Decimal("0"), trade_time=trade_time,
    )
