from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.services.positions_backfill import replay_cash


def _t(bar_time, trade_type, amount, commission="0.00", stamp_duty="0.00",
       price="10.00", quantity=100, code="000001.SZ", strategy_id=1):
    """构造与 BacktestTrade 同形的鸭子类型成交。"""
    return SimpleNamespace(
        bar_time=bar_time, trade_type=trade_type, stock_code=code,
        strategy_id=strategy_id, price=Decimal(price), quantity=quantity,
        amount=Decimal(amount), commission=Decimal(commission),
        stamp_duty=Decimal(stamp_duty),
    )


def test_replay_cash_includes_same_day_trade():
    """快照含当日成交：snap_date 当日的成交必须计入该日累计。"""
    trades = [_t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", "5.00")]
    out = replay_cash(trades, [date(2024, 1, 2), date(2024, 1, 3)])
    assert out[date(2024, 1, 2)] == Decimal("0")
    assert out[date(2024, 1, 3)] == Decimal("-1005.00")


def test_replay_cash_sell_deducts_fees():
    trades = [_t(datetime(2024, 1, 3, 10, 0), "SELL", "2000.00", "5.00", "2.00")]
    out = replay_cash(trades, [date(2024, 1, 3)])
    assert out[date(2024, 1, 3)] == Decimal("1993.00")


def test_replay_cash_accumulates_across_days():
    trades = [
        _t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", "5.00"),
        _t(datetime(2024, 1, 5, 10, 0), "SELL", "1200.00", "5.00", "2.00"),
    ]
    out = replay_cash(trades, [date(2024, 1, 3), date(2024, 1, 4),
                               date(2024, 1, 5)])
    assert out[date(2024, 1, 3)] == Decimal("-1005.00")
    assert out[date(2024, 1, 4)] == Decimal("-1005.00")
    assert out[date(2024, 1, 5)] == Decimal("-1005.00") + Decimal("1193.00")


def test_replay_cash_rejects_unknown_trade_type():
    # _cash_flow 按 .upper() 归一化（backtest_trades 实际只存大写 BUY/SELL），
    # 小写 "buy" 不会触发兜底，故这里用真正未知的取值来验证 ValueError。
    trades = [_t(datetime(2024, 1, 3, 10, 0), "HOLD", "1000.00")]
    with pytest.raises(ValueError):
        replay_cash(trades, [date(2024, 1, 3)])


def test_replay_cash_without_trades_is_all_zero():
    out = replay_cash([], [date(2024, 1, 2), date(2024, 1, 3)])
    assert set(out.values()) == {Decimal("0")}
