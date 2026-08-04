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


# ---------------------------------------------------------------------------
# 交易成本参数化（来自组合表，非硬编码）
# ---------------------------------------------------------------------------
def _order_open(stock_code, trade_type, quantity, price, bar_time=None):
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


def test_dispatcher_uses_portfolio_cost_params():
    """成本参数从组合表传入：min_commission=10、buy_rate=0.001、sell_rate=0.002、
    stamp_duty_rate=0.001、slippage=0.01。
    BUY 2000@10 → fill=10.1, amt=20200, comm=max(10,20.2)=20.2, stamp=0；
    SELL 2000@10 → fill=9.9, amt=19800, comm=max(10,39.6)=39.6, stamp=19.8。
    """
    dispatcher = SimulatedDispatcher(
        open_prices={"000001.SZ": Decimal("10")},
        min_commission=Decimal("10"),
        buy_commission_rate=Decimal("0.001"),
        sell_commission_rate=Decimal("0.002"),
        stamp_duty_rate=Decimal("0.001"),
        slippage=Decimal("0.01"),
    )
    buy_trade = dispatcher.place_order(
        _order_open("000001.SZ", TradeType.BUY, 2000, "10")
    )
    assert buy_trade.price == Decimal("10.1")
    assert buy_trade.amount == Decimal("10.1") * 2000
    assert buy_trade.commission == Decimal("20.2")
    assert buy_trade.stamp_duty == Decimal("0")

    sell_trade = dispatcher.place_order(
        _order_open("000001.SZ", TradeType.SELL, 2000, "10")
    )
    assert sell_trade.price == Decimal("9.9")
    assert sell_trade.amount == Decimal("9.9") * 2000
    assert sell_trade.commission == Decimal("39.6")
    assert sell_trade.stamp_duty == Decimal("19.8")


def test_dispatcher_min_commission_floor():
    """小单：amt=1000, rate=0.00025 → 算得 0.25 < min_commission=5 → 取 5。"""
    dispatcher = SimulatedDispatcher(
        open_prices={"000001.SZ": Decimal("10")},
        min_commission=Decimal("5"),
        buy_commission_rate=Decimal("0.00025"),
    )
    trade = dispatcher.place_order(
        _order_open("000001.SZ", TradeType.BUY, 100, "10")
    )
    assert trade.commission == Decimal("5")


def test_signal_name_propagates_order_to_trade():
    """OrderEvent.signal_name/signal_type 透传到 TradeEvent（交易明细定位信号来源）。
    公式信号（open_sig·OPEN）与风控信号（stop_loss·STOP_LOSS）两条路径各验一例。"""
    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})

    # 公式信号
    formula_order = OrderEvent(
        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",
        trade_type=TradeType.BUY, signal_type=SignalType.OPEN,
        signal_name="open_sig", quantity=100, price=Decimal("10"),
        bar_time=datetime(2026, 7, 30, 15, 0),
    )
    formula_trade = dispatcher.place_order(formula_order)
    assert formula_trade.signal_name == "open_sig"
    assert formula_trade.signal_type == SignalType.OPEN

    # 风控信号
    risk_order = OrderEvent(
        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",
        trade_type=TradeType.SELL, signal_type=SignalType.STOP_LOSS,
        signal_name="stop_loss", quantity=100, price=Decimal("10"),
        bar_time=datetime(2026, 7, 30, 15, 0),
    )
    risk_trade = dispatcher.place_order(risk_order)
    assert risk_trade.signal_name == "stop_loss"
    assert risk_trade.signal_type == SignalType.STOP_LOSS


def test_signal_name_defaults_empty_when_unset():
    """未传 signal_name 的 OrderEvent → TradeEvent.signal_name 为空串（旧调用不破）。"""
    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})
    trade = dispatcher.place_order(
        _order_open("000001.SZ", TradeType.BUY, 100, "10")  # 不传 signal_name
    )
    assert trade.signal_name == ""
