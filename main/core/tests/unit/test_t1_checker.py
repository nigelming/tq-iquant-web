from decimal import Decimal
from datetime import datetime, date

from core.engine.execution_engine import SimulatedT1Checker
from core.engine.position import Position
from core.engine.event import TradeEvent
from tq_iquant_shared.constants import TradeType


def _buy_trade(price, quantity, trade_time):
    return TradeEvent(
        strategy_id=1,
        portfolio_id=1,
        stock_code="000001.SZ",
        trade_type=TradeType.BUY,
        price=Decimal(price),
        quantity=quantity,
        amount=Decimal(price) * quantity,
        commission=Decimal("0"),
        stamp_duty=Decimal("0"),
        trade_time=trade_time,
    )


def test_t1_same_day_buy_not_sellable():
    checker = SimulatedT1Checker()
    pos = Position("000001.SZ")
    pos.apply_trade(_buy_trade("10", 1000, datetime(2026, 7, 30, 9, 30)))
    # 当天买入 → T+1 不可卖
    avail = checker.get_available_shares(pos, date(2026, 7, 30))
    assert avail == 0


def test_t1_next_day_sellable():
    checker = SimulatedT1Checker()
    pos = Position("000001.SZ")
    pos.apply_trade(_buy_trade("10", 1000, datetime(2026, 7, 29, 9, 30)))
    # 次日 → 可卖全部持仓
    avail = checker.get_available_shares(pos, date(2026, 7, 30))
    assert avail == 1000


def test_t1_no_position_zero():
    checker = SimulatedT1Checker()
    pos = Position("000001.SZ")
    avail = checker.get_available_shares(pos, date(2026, 7, 30))
    assert avail == 0


def test_t1_partial_sellable_after_partial_buy_next_day():
    checker = SimulatedT1Checker()
    pos = Position("000001.SZ")
    # 7-29 买 1000，7-30 又买 500（当日不可卖）→ 7-30 可卖仅 7-29 的 1000
    pos.apply_trade(_buy_trade("10", 1000, datetime(2026, 7, 29, 9, 30)))
    pos.apply_trade(_buy_trade("11", 500, datetime(2026, 7, 30, 9, 30)))
    avail = checker.get_available_shares(pos, date(2026, 7, 30))
    assert avail == 1000
