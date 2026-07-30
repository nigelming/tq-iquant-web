from decimal import Decimal
from datetime import datetime

from core.engine.position import Position
from core.engine.event import TradeEvent
from tq_iquant_shared.constants import TradeType


def test_buy_sell():
    pos = Position("000001.SZ")
    pos.buy(1000, Decimal("10"))
    assert pos.quantity == 1000
    assert pos.avg_cost == Decimal("10")
    pnl, amount = pos.sell(500, Decimal("12"))
    assert pnl > 0
    assert pos.quantity == 500


def test_avg_cost():
    pos = Position("000001.SZ")
    pos.buy(1000, Decimal("10"))
    pos.buy(500, Decimal("12"))
    assert pos.avg_cost == Decimal("10.66666666666666666666666667")


def _trade(trade_type, price, quantity, trade_time):
    return TradeEvent(
        strategy_id=1,
        portfolio_id=1,
        stock_code="000001.SZ",
        trade_type=trade_type,
        price=Decimal(price),
        quantity=quantity,
        amount=Decimal(price) * quantity,
        commission=Decimal("0"),
        stamp_duty=Decimal("0"),
        trade_time=trade_time,
    )


def test_apply_trade_buy_updates_quantity_avg_cost_highest():
    pos = Position("000001.SZ")
    t = datetime(2026, 7, 30, 9, 30)
    pos.apply_trade(_trade(TradeType.BUY, "10", 1000, t))
    assert pos.quantity == 1000
    assert pos.avg_cost == Decimal("10")
    assert pos.highest_price == Decimal("10")
    assert pos.buy_time == t


def test_apply_trade_buy_weighted_avg_cost_and_highest():
    pos = Position("000001.SZ")
    pos.apply_trade(_trade(TradeType.BUY, "10", 1000, datetime(2026, 7, 30, 9, 30)))
    pos.apply_trade(_trade(TradeType.BUY, "12", 500, datetime(2026, 7, 30, 10, 0)))
    assert pos.quantity == 1500
    assert pos.avg_cost == Decimal("10.66666666666666666666666667")
    assert pos.highest_price == Decimal("12")


def test_apply_trade_sell_reduces_quantity():
    pos = Position("000001.SZ")
    pos.apply_trade(_trade(TradeType.BUY, "10", 1000, datetime(2026, 7, 30, 9, 30)))
    pos.apply_trade(_trade(TradeType.SELL, "12", 400, datetime(2026, 7, 30, 14, 0)))
    assert pos.quantity == 600


def test_buy_time_records_first_buy_only():
    pos = Position("000001.SZ")
    first = datetime(2026, 7, 29, 9, 30)
    second = datetime(2026, 7, 30, 9, 30)
    pos.apply_trade(_trade(TradeType.BUY, "10", 1000, first))
    pos.apply_trade(_trade(TradeType.BUY, "11", 500, second))
    assert pos.buy_time == first


def test_can_sell_t_plus_one():
    """T+1: 当天买入不可卖，昨天及之前买入可卖。"""
    pos = Position("000001.SZ")
    buy_day = datetime(2026, 7, 29, 9, 30)
    pos.apply_trade(_trade(TradeType.BUY, "10", 1000, buy_day))
    # 同一交易日卖出 → 不可卖
    assert pos.can_sell_on(buy_day.date()) is False
    # 下一交易日 → 可卖
    assert pos.can_sell_on(datetime(2026, 7, 30).date()) is True
