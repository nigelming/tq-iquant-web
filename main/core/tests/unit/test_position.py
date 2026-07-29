from decimal import Decimal
from core.engine.position import Position


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
