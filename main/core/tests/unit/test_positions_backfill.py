from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.services.positions_backfill import (replay_cash, replay_positions,
                                             value_positions)


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


def test_replay_cash_accepts_lowercase_trade_type():
    """小写 trade_type 经 .upper() 归一化后正常处理，不抛错。"""
    trades = [_t(datetime(2024, 1, 3, 10, 0), "buy", "1000.00", "5.00")]
    out = replay_cash(trades, [date(2024, 1, 3)])
    assert out[date(2024, 1, 3)] == Decimal("-1005.00")


def test_replay_cash_without_trades_is_all_zero():
    out = replay_cash([], [date(2024, 1, 2), date(2024, 1, 3)])
    assert set(out.values()) == {Decimal("0")}


def test_replay_positions_t1_blocks_same_day_sellable():
    """T+1：买入当日的份额在当日快照不可卖，次日才可卖。"""
    trades = [_t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", quantity=100)]
    out = replay_positions(trades, [date(2024, 1, 3), date(2024, 1, 4)])
    d3 = out[date(2024, 1, 3)]
    assert d3[0]["quantity"] == 100
    assert d3[0]["sellable_quantity"] == 0
    assert out[date(2024, 1, 4)][0]["sellable_quantity"] == 100


def test_replay_positions_cost_value_uses_avg_cost():
    """cost_value = avg_cost × quantity，不扣佣金（与引擎 Position.buy 一致）。"""
    trades = [
        _t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", price="10.00",
            quantity=100),
        _t(datetime(2024, 1, 4, 10, 0), "BUY", "1200.00", price="12.00",
            quantity=100),
    ]
    slot = replay_positions(trades, [date(2024, 1, 4)])[date(2024, 1, 4)][0]
    assert slot["quantity"] == 200
    assert slot["cost_value"] == "2200.00"  # 均价 11.00 × 200
    assert slot["market_value"] == "0"      # 估值由 value_positions 填


def test_replay_positions_sell_is_fifo_and_flattens():
    """卖出按最早买入桶 FIFO 扣减；清仓后当日快照为空仓。"""
    trades = [
        _t(datetime(2024, 1, 2, 10, 0), "BUY", "1000.00", price="10.00",
            quantity=100),
        _t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", price="10.00",
            quantity=100),
        _t(datetime(2024, 1, 4, 10, 0), "SELL", "1500.00", price="15.00",
            quantity=150),
        _t(datetime(2024, 1, 5, 10, 0), "SELL", "750.00", price="15.00",
            quantity=50),
    ]
    out = replay_positions(trades, [date(2024, 1, 3), date(2024, 1, 4),
                                    date(2024, 1, 5)])
    assert out[date(2024, 1, 3)][0]["quantity"] == 200
    # 150 股卖光 1/2 桶（100），剩 50 股仍来自 1/3 桶 → 1/3 买入在 1/4 已过 T+1，可卖
    held = out[date(2024, 1, 4)][0]
    assert held["quantity"] == 50
    assert held["sellable_quantity"] == 50
    assert out[date(2024, 1, 5)] == []


def test_replay_positions_aggregates_across_strategies():
    """同一只股票被两个策略持有 → 组合层按 stock_code 聚合，均价各自独立。"""
    trades = [
        _t(datetime(2024, 1, 2, 10, 0), "BUY", "1000.00", price="10.00",
            quantity=100, strategy_id=1),
        _t(datetime(2024, 1, 2, 10, 0), "BUY", "2000.00", price="20.00",
            quantity=100, strategy_id=2),
    ]
    out = replay_positions(trades, [date(2024, 1, 3)])
    assert len(out[date(2024, 1, 3)]) == 1
    slot = out[date(2024, 1, 3)][0]
    assert slot["quantity"] == 200
    assert slot["sellable_quantity"] == 200
    assert slot["cost_value"] == "3000.00"  # 10.00×100 + 20.00×100


def test_replay_positions_sorted_by_code_and_empty_before_first_trade():
    trades = [
        _t(datetime(2024, 1, 3, 10, 0), "BUY", "1000.00", price="10.00",
            quantity=100, code="600000.SH"),
        _t(datetime(2024, 1, 3, 10, 0), "BUY", "500.00", price="5.00",
            quantity=100, code="000001.SZ"),
    ]
    out = replay_positions(trades, [date(2024, 1, 2), date(2024, 1, 3)])
    assert out[date(2024, 1, 2)] == []
    assert [s["stock_code"] for s in out[date(2024, 1, 3)]] == [
        "000001.SZ", "600000.SH"]


def _slot(code="000001.SZ", qty=100, sellable=100, cost="1000.00"):
    return {"stock_code": code, "quantity": qty, "sellable_quantity": sellable,
            "market_value": "0", "cost_value": cost}


def test_value_positions_uses_exact_date_price():
    d = date(2024, 1, 3)
    out, notes = value_positions(
        [_slot()], {"000001.SZ": {d: Decimal("10")}}, d, Decimal("1000"))
    assert out[0]["market_value"] == "1000.00"   # 量化到 2 位
    assert notes["carried"] == 0
    assert notes["cost_fallback"] == 0
    assert notes["k"] == Decimal("1")
    assert notes["unscaled"] is False


def test_value_positions_carries_forward_last_price():
    """停牌/缺 bar → 结转该股最近一个已知收盘价。"""
    d = date(2024, 1, 4)
    out, notes = value_positions(
        [_slot()],
        {"000001.SZ": {date(2024, 1, 2): Decimal("10"),
                       date(2024, 1, 3): Decimal("11")}},
        d, Decimal("1100"))
    assert out[0]["market_value"] == "1100.00"   # 结转 1/3 的 11 × 100
    assert notes["carried"] == 1
    assert notes["k"] == Decimal("1")


def test_value_positions_falls_back_to_avg_cost_when_no_price_at_all():
    """该股全程无价 → 用 avg_cost(=cost_value/quantity) 兜底，与 positions_detail 一致。"""
    d = date(2024, 1, 3)
    out, notes = value_positions([_slot()], {}, d, Decimal("1000"))
    assert out[0]["market_value"] == "1000.00"
    assert notes["cost_fallback"] == 1
    assert notes["no_price"] == ["000001.SZ"]


def test_value_positions_scales_to_stored_market_value():
    """事后前复权价有除权漂移 → 缩放到快照存量市值，使行业权重之和为 100%。"""
    d = date(2024, 1, 3)
    detail = [_slot("000001.SZ", 100, cost="1000.00"),
              _slot("600000.SH", 100, cost="2000.00")]
    close = {"000001.SZ": {d: Decimal("9")}, "600000.SH": {d: Decimal("18")}}
    out, notes = value_positions(detail, close, d, Decimal("3000"))
    # 重算 raw = 900 + 1800 = 2700 → k = 3000/2700（无限循环小数）
    assert notes["k"] == Decimal("3000") / Decimal("2700")
    # 量化到 2 位后恰好回到存量市值
    assert [s["market_value"] for s in out] == ["1000.00", "2000.00"]
    assert sum(Decimal(s["market_value"]) for s in out) == Decimal("3000.00")


def test_value_positions_does_not_scale_when_stored_missing():
    d = date(2024, 1, 3)
    out, notes = value_positions(
        [_slot()], {"000001.SZ": {d: Decimal("10")}}, d, None)
    assert out[0]["market_value"] == "1000.00"
    assert notes["k"] is None
    assert notes["unscaled"] is True


def test_value_positions_does_not_mutate_input():
    d = date(2024, 1, 3)
    detail = [_slot()]
    value_positions(detail, {"000001.SZ": {d: Decimal("10")}}, d,
                    Decimal("1000"))
    assert detail[0]["market_value"] == "0"


def test_value_positions_empty_detail_is_noop():
    d = date(2024, 1, 3)
    out, notes = value_positions([], {}, d, Decimal("0"))
    assert out == []
    assert notes["k"] is None
    assert notes["carried"] == 0


def test_value_positions_treats_nonpositive_close_as_missing():
    """行情源给出 0 收盘价（停牌占位）时按无价处理，回退 avg_cost。

    与 `positions_detail()` 的 `price is None or price <= 0 → pos.avg_cost` 口径一致
    （`portfolio.py:475`）；否则该槽市值为 0，会把 raw_sum 压小、连带放大其余持仓的 k。
    """
    d = date(2024, 1, 3)
    out, notes = value_positions(
        [_slot()], {"000001.SZ": {d: Decimal("0")}}, d, Decimal("1000"))
    assert out[0]["market_value"] == "1000.00"
    assert notes["cost_fallback"] == 1
    assert notes["no_price"] == []      # 序列存在，只是当日价不可用
