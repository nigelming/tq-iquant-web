import json
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from core.models import (BacktestDailySnapshot, BacktestRecord, BacktestTrade,
                         Formula, PortfolioStrategy, StockPool, StockPoolStock,
                         Strategy)
from core.services.positions_backfill import (backfill_record, replay_cash,
                                              replay_positions, value_positions)


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


def test_value_positions_raw_sum_nonpositive_exits_unscaled():
    """qty=0 且全程无价 → raw_sum=0 的异常出口：k=None、unscaled=False，不抛异常。

    Task 4 编排层的「raw_sum<=0 拒绝」门禁依赖此出口可被精确识别
    （与空仓出口 detail==[] 区分：两者 k 同为 None，但 detail 非空）。
    """
    d = date(2024, 1, 3)
    out, notes = value_positions(
        [_slot(qty=0, cost="0.00")], {}, d, Decimal("1000"))
    assert out[0]["market_value"] == "0.00"
    assert notes["k"] is None
    assert notes["unscaled"] is False
    assert notes["cost_fallback"] == 1


def test_value_positions_nonpositive_carried_falls_back_to_avg_cost():
    """唯一已知价 <=0（停牌占位）→ 不算有效结转，回退 avg_cost 而非按 0 结转。

    与 `price <= 0` 同口径：0 价结转会把 raw_sum 压到 0，触发异常出口。
    """
    d = date(2024, 1, 3)
    out, notes = value_positions(
        [_slot()], {"000001.SZ": {date(2024, 1, 2): Decimal("0")}},
        d, Decimal("1000"))
    assert notes["carried"] == 0
    assert notes["cost_fallback"] == 1
    assert out[0]["market_value"] == "1000.00"   # avg_cost 10 × 100


def test_value_positions_never_uses_future_price_as_carry():
    """结转价只取 <= snap_date 的 bar；未来价格不得参与估值。"""
    d = date(2024, 1, 3)
    out, notes = value_positions(
        [_slot()], {"000001.SZ": {date(2024, 1, 4): Decimal("20")}},
        d, Decimal("1000"))
    assert notes["carried"] == 0
    assert notes["cost_fallback"] == 1
    assert out[0]["market_value"] == "1000.00"   # 回退 avg_cost，未用 1/4 的 20


def _seed_record(db):
    pool = StockPool(code="BF", name="bf_pool")
    db.add(pool)
    db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="000001.SZ",
                          stock_name="平安银行"))
    formula = Formula(name="f", content="REF(CLOSE,1)")
    db.add(formula)
    db.flush()
    ps = PortfolioStrategy(name="ps", stock_pool_id=pool.id,
                           initial_capital=Decimal("10000"),
                           max_drawdown=Decimal("0.2"),
                           daily_loss_limit=Decimal("0.05"))
    db.add(ps)
    db.flush()
    strat = Strategy(portfolio_id=ps.id, name="s1", formula_id=formula.id,
                     period="1d", role="master", capital_ratio=Decimal("1"),
                     max_positions=5, stop_loss_ratio=Decimal("0.05"),
                     take_profit_ratio=Decimal("0.2"),
                     trailing_stop_ratio=Decimal("0"))
    db.add(strat)
    db.flush()
    rec = BacktestRecord(portfolio_strategy_id=ps.id, name="bf",
                         start_date=date(2024, 1, 2), end_date=date(2024, 1, 5),
                         status="completed", progress=100)
    db.add(rec)
    db.commit()
    return rec, ps, strat


def _snap(db, rec, d, cash, market_value):
    db.add(BacktestDailySnapshot(
        backtest_record_id=rec.id, target_type="portfolio",
        target_id=rec.portfolio_strategy_id, snap_date=d,
        total_value=Decimal(str(cash)) + Decimal(str(market_value)),
        cash=Decimal(str(cash)), market_value=Decimal(str(market_value))))


def _trade(db, rec, strat, bar_time, kind, price, quantity, amount,
           commission="0.00", stamp_duty="0.00"):
    db.add(BacktestTrade(
        backtest_record_id=rec.id, strategy_id=strat.id,
        signal_name="open_sig", signal_type="OPEN", stock_code="000001.SZ",
        trade_type=kind, price=Decimal(price), quantity=quantity,
        amount=Decimal(amount), commission=Decimal(commission),
        stamp_duty=Decimal(stamp_duty), bar_time=bar_time))


D1, D2, D3, D4 = (date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4),
                  date(2024, 1, 5))


def _seed_backfillable(db):
    """4 天现金自洽链：1/2 空仓 10000 → 1/3 买 100@10 → 1/4 加仓 100@12 → 1/5 持有不动。"""
    rec, _ps, strat = _seed_record(db)
    _snap(db, rec, D1, "10000.00", "0.00")
    _snap(db, rec, D2, "9000.00", "1000.00")
    _snap(db, rec, D3, "7800.00", "2200.00")
    _snap(db, rec, D4, "7800.00", "2200.00")
    _trade(db, rec, strat, datetime(2024, 1, 3, 15, 0), "BUY", "10.00", 100,
           "1000.00")
    _trade(db, rec, strat, datetime(2024, 1, 4, 15, 0), "BUY", "12.00", 100,
           "1200.00")
    db.commit()
    return rec


def _close_two_days(codes, start, end):
    """价格 9/11 相对成本 10/12 有 -10% 漂移 → 缩放后市值仍等于存量市值。"""
    return {"000001.SZ": {D2: Decimal("9"), D3: Decimal("11")}}


def _portfolio_snaps(db, rec):
    return (db.query(BacktestDailySnapshot)
            .filter_by(backtest_record_id=rec.id, target_type="portfolio")
            .order_by(BacktestDailySnapshot.snap_date).all())


def test_backfill_record_writes_positions_json(db_session):
    rec = _seed_backfillable(db_session)
    out = backfill_record(db_session, rec.id, close_provider=_close_two_days)
    assert out["status"] == "ok"
    assert out["written"] is True
    assert out["cash_check"] == {"ok": True, "mismatches": 0,
                                 "first_mismatch": None}
    snaps = _portfolio_snaps(db_session, rec)
    assert [json.loads(s.positions_json) for s in snaps] == [
        [],
        [{"stock_code": "000001.SZ", "quantity": 100, "sellable_quantity": 0,
          "market_value": "1000.00", "cost_value": "1000.00"}],
        [{"stock_code": "000001.SZ", "quantity": 200, "sellable_quantity": 100,
          "market_value": "2200.00", "cost_value": "2200.00"}],
        [{"stock_code": "000001.SZ", "quantity": 200, "sellable_quantity": 200,
          "market_value": "2200.00", "cost_value": "2200.00"}],
    ]


def test_backfill_record_uses_carried_forward_price(db_session):
    """1/5 无 bar → 结转 1/4 的 11；首日空仓不参与缩放。"""
    rec = _seed_backfillable(db_session)
    out = backfill_record(db_session, rec.id, close_provider=_close_two_days)
    assert out["carry_forward_days"] == 1
    assert out["scale"]["no_scale_days"] == 1
    assert out["scale"]["max_dev"][:6] == "0.1111"  # 1/3 时 k = 1000/900


def test_backfill_record_reports_missing_price_codes(db_session):
    rec = _seed_backfillable(db_session)
    out = backfill_record(db_session, rec.id, close_provider=lambda *a: {})
    assert out["status"] == "ok"
    assert out["missing_price_codes"] == ["000001.SZ"]
    assert out["avg_cost_fallback"] == 3   # 1/3、1/4、1/5 三个有持仓的快照日


def test_backfill_record_rejects_scale_deviation_over_five_percent(db_session):
    """重建市值与存量偏离 >5% → 拒绝写入，DB 一行不写。"""
    rec = _seed_backfillable(db_session)

    def _drifted(codes, start, end):
        return {"000001.SZ": {D2: Decimal("5"), D3: Decimal("5.5")}}

    out = backfill_record(db_session, rec.id, close_provider=_drifted)
    assert out["status"] == "skipped"
    assert out["written"] is False
    assert Decimal(out["scale"]["max_dev"]) > Decimal("0.05")
    rows = _portfolio_snaps(db_session, rec)
    assert all(s.positions_json is None for s in rows)


def test_backfill_record_force_overrides_scale_gate(db_session):
    rec = _seed_backfillable(db_session)

    def _drifted(codes, start, end):
        return {"000001.SZ": {D2: Decimal("5"), D3: Decimal("5.5")}}

    out = backfill_record(db_session, rec.id, force=True,
                          close_provider=_drifted)
    assert out["status"] == "ok"
    assert out["written"] is True


def test_backfill_record_accepts_trade_on_first_snapshot_day(db_session):
    """首条快照当日已含成交仍须通过——差分校验看 cumflow[首日] 而非直接比 cumflow。"""
    rec, _ps, strat = _seed_record(db_session)
    _snap(db_session, rec, D1, "9000.00", "1000.00")
    _snap(db_session, rec, D2, "9000.00", "1000.00")
    _trade(db_session, rec, strat, datetime(2024, 1, 2, 15, 0), "BUY", "10.00",
           100, "1000.00")
    db_session.commit()
    out = backfill_record(db_session, rec.id, close_provider=_close_two_days)
    assert out["cash_check"]["ok"] is True
    assert out["status"] == "ok"


def test_backfill_record_fails_on_cash_mismatch(db_session):
    """现金差分校验不通过 → 整条记录失败，一行不写（硬门限）。"""
    rec, _ps, strat = _seed_record(db_session)
    _snap(db_session, rec, D1, "1000.00", "0.00")
    _snap(db_session, rec, D2, "-900.00", "1000.00")   # 与 -1000 的现金流不自洽
    _trade(db_session, rec, strat, datetime(2024, 1, 3, 15, 0), "BUY", "10.00",
           100, "1000.00")
    db_session.commit()
    out = backfill_record(db_session, rec.id, close_provider=_close_two_days)
    assert out["status"] == "failed"
    assert out["cash_check"]["ok"] is False
    assert out["cash_check"]["mismatches"] == 1
    assert out["cash_check"]["first_mismatch"] == "2024-01-03"
    rows = _portfolio_snaps(db_session, rec)
    assert all(s.positions_json is None for s in rows)


def test_backfill_record_missing_record_returns_failed(db_session):
    out = backfill_record(db_session, 9999, close_provider=_close_two_days)
    assert out["status"] == "failed"
    assert out["error"] == "回测记录不存在"


def test_backfill_record_without_snapshots_returns_failed(db_session):
    rec, _ps, _strat = _seed_record(db_session)
    out = backfill_record(db_session, rec.id, close_provider=_close_two_days)
    assert out["status"] == "failed"
    assert "无组合日快照" in out["error"]


def test_backfill_record_rejects_nonpositive_replayed_market_value(db_session):
    """有持仓但重放市值非正（raw_sum<=0）→ 整条拒绝，一行不写。

    value_positions 的异常出口（k=None 且 unscaled=False 且 detail 非空）
    必须与空仓出口区分：空仓正常通过，此路径拒绝。
    """
    rec, _ps, strat = _seed_record(db_session)
    _snap(db_session, rec, D1, "10000.00", "0.00")
    _snap(db_session, rec, D2, "10000.00", "1000.00")
    _trade(db_session, rec, strat, datetime(2024, 1, 3, 15, 0), "BUY", "0.00",
           100, "0.00")   # 0 价买入 → avg_cost=0 → 无价时兜底价为 0
    db_session.commit()
    out = backfill_record(db_session, rec.id, close_provider=lambda *a: {})
    assert out["status"] == "failed"
    assert out["written"] is False
    rows = _portfolio_snaps(db_session, rec)
    assert all(s.positions_json is None for s in rows)


def test_backfill_record_skips_populated_unless_force(db_session):
    """positions_json 已回填 → 默认跳过；force=True 重跑覆盖。"""
    rec = _seed_backfillable(db_session)
    first = backfill_record(db_session, rec.id, close_provider=_close_two_days)
    assert first["status"] == "ok"
    assert first["written"] is True
    second = backfill_record(db_session, rec.id,
                             close_provider=_close_two_days)
    assert second["status"] == "skipped"
    assert second["written"] is False
    forced = backfill_record(db_session, rec.id, force=True,
                             close_provider=_close_two_days)
    assert forced["status"] == "ok"
    assert forced["written"] is True
