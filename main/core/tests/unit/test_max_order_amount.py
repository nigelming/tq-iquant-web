"""单笔最大买入金额（策略级 max_order_amount，回测/实盘双端 cap_quantity）。

覆盖：模型列（nullable 默认 None）、cap_quantity BUY 金额封顶缩量/不限/
不足 1 手拒单 + order_amount_cap 决策事件、execute 透传、StrategyContext
默认与 builder 透传、service 序列化/创建/更新透传、API 校验（非正数 400）。
迁移 up-down 见 integration/test_strategy_execution_migration.py。
"""
from datetime import datetime
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.api.strategies import StrategyCreate, _validate_strategy
from core.engine.account import Account
from core.engine.decision import DecisionRecorder
from core.engine.event import OrderEvent
from core.engine.execution_engine import ExecutionEngine, LiveT1Checker, SimulatedDispatcher
from core.engine.portfolio_builder import assemble_portfolio
from core.engine.strategy_context import StrategyContext
from core.models import Base, Formula, PortfolioStrategy, Strategy, StockPool
from core.services.strategy_service import (
    create_strategy, serialize_strategy, update_strategy,
)
from tq_iquant_shared.constants import SignalType, TradeType


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db):
    pool = StockPool(code="P1", name="p")
    f = Formula(name="F", content="REF(CLOSE,1)")
    db.add_all([pool, f])
    db.commit()
    return pool.id, f.id


def _find(events, gate):
    for ev in events:
        if ev.gate == gate:
            return ev
    return None


def _order(trade_type, quantity, *, price="10", code="000001.SZ", sid=1):
    return OrderEvent(
        strategy_id=sid, portfolio_id=1, stock_code=code,
        trade_type=trade_type,
        signal_type=SignalType.OPEN if trade_type == TradeType.BUY else SignalType.CLOSE,
        signal_name="t", quantity=quantity,
        price=Decimal(price) if price is not None else None,
        bar_time=datetime(2026, 7, 30, 15, 0),
    )


# ---------------------------------------------------------------------------
# 模型列（nullable 默认 None = 不限）
# ---------------------------------------------------------------------------
def test_model_column_default_none_and_persists_explicit_value():
    db = _db()
    pool_id, formula_id = _seed(db)
    ps = PortfolioStrategy(
        name="ps", stock_pool_id=pool_id, initial_capital=Decimal("100000"),
    )
    db.add(ps)
    db.flush()
    s1 = Strategy(
        portfolio_id=ps.id, name="S1", formula_id=formula_id,
        period="1d", role="independent",
    )
    s2 = Strategy(
        portfolio_id=ps.id, name="S2", formula_id=formula_id,
        period="1d", role="independent", max_order_amount=200000.0,
    )
    db.add_all([s1, s2])
    db.commit()
    db.refresh(s1)
    db.refresh(s2)
    assert s1.max_order_amount is None  # 默认不限
    assert s2.max_order_amount == 200000.0
    db.close()


# ---------------------------------------------------------------------------
# cap_quantity —— BUY 金额封顶（回测/实盘共用同一闸门）
# ---------------------------------------------------------------------------
def test_cap_quantity_shrinks_buy_to_amount_cap_lot_multiple():
    """金额 2 万/价 100/lot 100 → 200 股；请求 300 → 缩至 200 + order_amount_cap 事件。"""
    r = DecisionRecorder()
    acc = Account(initial_capital=Decimal("1000000"))  # 现金充足，仅金额上限约束
    eng = ExecutionEngine(None, LiveT1Checker(), recorder=r)
    out = eng.cap_quantity(
        _order(TradeType.BUY, 300, price="100"), acc, None,
        max_order_amount=Decimal("20000"),
    )
    assert out == 200
    ev = _find(r.drain(), "order_amount_cap")
    assert ev is not None
    assert ev.layer == "capital_gate" and ev.action == "shrink"
    assert ev.requested_qty == 300 and ev.final_qty == 200
    assert ev.param_name == "max_order_amount" and ev.param_value == 20000.0
    assert "单笔最大金额" in ev.message


def test_cap_quantity_none_means_unlimited():
    """max_order_amount=None（默认）→ 不限，旧行为逐字节一致。"""
    r = DecisionRecorder()
    acc = Account(initial_capital=Decimal("1000000"))
    eng = ExecutionEngine(None, LiveT1Checker(), recorder=r)
    out = eng.cap_quantity(
        _order(TradeType.BUY, 300, price="100"), acc, None,
        max_order_amount=None,
    )
    assert out == 300
    assert r.drain() == []


def test_cap_quantity_non_binding_records_nothing():
    """请求金额未超上限 → 不缩量、无事件。"""
    r = DecisionRecorder()
    acc = Account(initial_capital=Decimal("1000000"))
    eng = ExecutionEngine(None, LiveT1Checker(), recorder=r)
    out = eng.cap_quantity(
        _order(TradeType.BUY, 100, price="100"), acc, None,
        max_order_amount=Decimal("20000"),
    )
    assert out == 100
    assert r.drain() == []


def test_cap_quantity_below_one_lot_rejects():
    """上限买不够 1 手（5000 元/价 100 → 0 整手）→ 拒单 + order_amount_cap reject。"""
    r = DecisionRecorder()
    acc = Account(initial_capital=Decimal("1000000"))
    eng = ExecutionEngine(None, LiveT1Checker(), recorder=r)
    out = eng.cap_quantity(
        _order(TradeType.BUY, 300, price="100"), acc, None,
        max_order_amount=Decimal("5000"),
    )
    assert out is None
    ev = _find(r.drain(), "order_amount_cap")
    assert ev is not None
    assert ev.layer == "capital_gate" and ev.action == "reject"
    assert ev.final_qty == 0
    assert "单笔最大金额" in ev.message


def test_cap_quantity_sell_path_ignores_amount_cap():
    """仅 BUY 生效：SELL 不受金额上限约束。"""
    r = DecisionRecorder()
    acc = Account(initial_capital=Decimal("0"))
    t1 = LiveT1Checker()
    t1.set_available_map({"000001.SZ": 500})
    eng = ExecutionEngine(None, t1, recorder=r)
    from core.engine.position import Position
    position = Position("000001.SZ")
    position.quantity = 1000
    out = eng.cap_quantity(
        _order(TradeType.SELL, 500, price="100"), acc, position,
        max_order_amount=Decimal("20000"),
    )
    assert out == 500  # T+1 可卖充足，全量卖出
    assert r.drain() == []  # 卖出无金额事件


def test_execute_forwards_amount_cap_to_cap_quantity():
    """execute（回测 apply=True 路径）透传 max_order_amount：300 股 → 200 股成交。"""
    acc = Account(initial_capital=Decimal("1000000"))
    pos = None
    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("100")})
    eng = ExecutionEngine(dispatcher, LiveT1Checker())
    order = _order(TradeType.BUY, 300, price="100")
    trade = eng.execute(order, acc, pos, max_order_amount=Decimal("20000"))
    assert trade is not None
    assert trade.quantity == 200
    assert order.quantity == 200


# ---------------------------------------------------------------------------
# StrategyContext 默认 + builder 透传
# ---------------------------------------------------------------------------
def test_strategy_context_amount_cap_default_none():
    ctx = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5,
    )
    assert ctx.max_order_amount is None


def test_builder_passes_amount_cap_to_context():
    db = _db()
    pool_id, formula_id = _seed(db)
    ps = PortfolioStrategy(
        name="ps", stock_pool_id=pool_id, initial_capital=Decimal("100000"),
    )
    db.add(ps)
    db.flush()
    s1 = Strategy(
        portfolio_id=ps.id, name="S1", formula_id=formula_id,
        period="1d", role="independent", max_order_amount=200000.0,
    )
    s2 = Strategy(
        portfolio_id=ps.id, name="S2", formula_id=formula_id,
        period="1d", role="independent",  # None=不限
    )
    db.add_all([s1, s2])
    db.commit()
    port = assemble_portfolio(ps, [s1, s2], db)
    assert port.strategies[0].max_order_amount == Decimal("200000")
    assert port.strategies[1].max_order_amount is None
    db.close()


# ---------------------------------------------------------------------------
# service 序列化 / 创建 / 更新透传
# ---------------------------------------------------------------------------
def test_serialize_strategy_includes_max_order_amount():
    db = _db()
    pool_id, formula_id = _seed(db)
    ps = PortfolioStrategy(
        name="ps", stock_pool_id=pool_id, initial_capital=Decimal("100000"),
    )
    db.add(ps)
    db.flush()
    s = Strategy(
        portfolio_id=ps.id, name="S", formula_id=formula_id,
        period="1d", role="independent", max_order_amount=200000.0,
    )
    db.add(s)
    db.commit()
    data = serialize_strategy(s)
    assert data["max_order_amount"] == 200000.0
    s.max_order_amount = None
    assert serialize_strategy(s)["max_order_amount"] is None
    db.close()


def test_service_create_update_roundtrip_amount_cap():
    db = _db()
    pool_id, formula_id = _seed(db)
    req = StrategyCreate(max_order_amount=200000.0, **dict(
        name="S", formula_id=formula_id, period="1d", role="independent",
    ))
    data = create_strategy(db, pool_id, req)
    sid = data["id"]
    assert data["max_order_amount"] == 200000.0
    upd = StrategyCreate(**dict(
        name="S", formula_id=formula_id, period="1d", role="independent",
    ))  # 未携带 → None=不限
    data2 = update_strategy(db, pool_id, sid, upd)
    assert data2["max_order_amount"] is None
    db.close()


# ---------------------------------------------------------------------------
# API 校验（None 放行，非 None 须 > 0）
# ---------------------------------------------------------------------------
def _strategy_base(formula_id):
    return dict(name="S", formula_id=formula_id, period="1d", role="independent")


def test_api_rejects_non_positive_max_order_amount():
    db = _db()
    pool_id, formula_id = _seed(db)
    for bad_value in (-0.01, 0):
        bad = StrategyCreate(max_order_amount=bad_value, **_strategy_base(formula_id))
        msg = _validate_strategy(bad, db, pool_id)
        assert msg is not None and "单笔最大金额须大于 0" in msg, (bad_value, msg)
    db.close()


def test_api_accepts_none_and_positive_max_order_amount():
    db = _db()
    pool_id, formula_id = _seed(db)
    ok_none = StrategyCreate(**_strategy_base(formula_id))
    assert _validate_strategy(ok_none, db, pool_id) is None
    ok_pos = StrategyCreate(max_order_amount=200000.0, **_strategy_base(formula_id))
    assert _validate_strategy(ok_pos, db, pool_id) is None
    db.close()
