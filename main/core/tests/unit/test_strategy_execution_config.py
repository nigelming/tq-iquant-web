"""批次1：策略级实盘执行配置管线单测（重下/拆单/风控时点）。

5 个新配置向后兼容（默认关/保守默认）：
- requote_enabled / requote_max_attempts：撤单后有限追价重下
- split_order_enabled / split_order_threshold：TDX 大额拆单（本批次仅落配置管线）
- risk_timing：风控/信号执行时点（next_day 默认 / immediate）

覆盖：模型列默认值、StrategyContext 默认与透传、API 校验（400 语义，
经 _validate_strategy）、service 序列化/创建/更新透传、assemble_portfolio
builder 透传。迁移 up-down 见 integration/test_strategy_execution_migration.py。
"""
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.api.strategies import StrategyCreate, _validate_strategy
from core.engine.portfolio_builder import assemble_portfolio
from core.engine.strategy_context import StrategyContext
from core.models import Base, Formula, PortfolioStrategy, Strategy, StockPool
from core.services.strategy_service import (
    create_strategy, serialize_strategy, update_strategy,
)


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


# ---------------------------------------------------------------------------
# 模型列默认值（向后兼容：旧行为不变）
# ---------------------------------------------------------------------------
def test_model_columns_defaults():
    db = _db()
    pool_id, formula_id = _seed(db)
    ps = PortfolioStrategy(
        name="ps", stock_pool_id=pool_id, initial_capital=Decimal("100000"),
    )
    db.add(ps)
    db.flush()
    s = Strategy(
        portfolio_id=ps.id, name="S", formula_id=formula_id,
        period="1d", role="independent",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    assert s.requote_enabled is False
    assert s.requote_max_attempts == 2
    assert s.split_order_enabled is False
    assert s.split_order_threshold == 500000.0
    assert s.risk_timing == "next_day"
    db.close()


def test_model_columns_persist_explicit_values():
    db = _db()
    pool_id, formula_id = _seed(db)
    ps = PortfolioStrategy(
        name="ps", stock_pool_id=pool_id, initial_capital=Decimal("100000"),
    )
    db.add(ps)
    db.flush()
    s = Strategy(
        portfolio_id=ps.id, name="S", formula_id=formula_id,
        period="1d", role="independent",
        requote_enabled=True, requote_max_attempts=3,
        split_order_enabled=True, split_order_threshold=300000.0,
        risk_timing="immediate",
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    assert s.requote_enabled is True
    assert s.requote_max_attempts == 3
    assert s.split_order_enabled is True
    assert s.split_order_threshold == 300000.0
    assert s.risk_timing == "immediate"
    db.close()


# ---------------------------------------------------------------------------
# API 校验（400 语义）
# ---------------------------------------------------------------------------
def _strategy_base(formula_id):
    return dict(name="S", formula_id=formula_id, period="1d", role="independent")


def test_api_rejects_requote_attempts_out_of_range():
    db = _db()
    pool_id, formula_id = _seed(db)
    bad = StrategyCreate(requote_max_attempts=11, **_strategy_base(formula_id))
    assert "requote_max_attempts" in _validate_strategy(bad, db, pool_id)
    bad2 = StrategyCreate(requote_max_attempts=-1, **_strategy_base(formula_id))
    assert "requote_max_attempts" in _validate_strategy(bad2, db, pool_id)
    db.close()


def test_api_rejects_negative_split_threshold():
    db = _db()
    pool_id, formula_id = _seed(db)
    bad = StrategyCreate(split_order_threshold=-0.01, **_strategy_base(formula_id))
    assert "split_order_threshold" in _validate_strategy(bad, db, pool_id)
    db.close()


def test_api_rejects_unknown_risk_timing():
    db = _db()
    pool_id, formula_id = _seed(db)
    bad = StrategyCreate(risk_timing="same_day", **_strategy_base(formula_id))
    assert "risk_timing" in _validate_strategy(bad, db, pool_id)
    db.close()


def test_api_accepts_valid_execution_config():
    db = _db()
    pool_id, formula_id = _seed(db)
    ok = StrategyCreate(
        requote_enabled=True, requote_max_attempts=3,
        split_order_enabled=True, split_order_threshold=0,
        risk_timing="immediate", **_strategy_base(formula_id),
    )
    assert _validate_strategy(ok, db, pool_id) is None
    db.close()


# ---------------------------------------------------------------------------
# service 序列化 / 创建 / 更新透传
# ---------------------------------------------------------------------------
def test_serialize_strategy_includes_execution_config_with_fallbacks():
    db = _db()
    pool_id, formula_id = _seed(db)
    ps = PortfolioStrategy(
        name="ps", stock_pool_id=pool_id, initial_capital=Decimal("100000"),
    )
    db.add(ps)
    db.flush()
    s = Strategy(
        portfolio_id=ps.id, name="S", formula_id=formula_id,
        period="1d", role="independent",
    )
    db.add(s)
    db.commit()
    data = serialize_strategy(s)
    assert data["requote_enabled"] is False
    assert data["requote_max_attempts"] == 2
    assert data["split_order_enabled"] is False
    assert data["split_order_threshold"] == 500000.0
    assert data["risk_timing"] == "next_day"
    # 模拟历史行 NULL（server_default 兜底前的脏数据）→ 序列化回落默认
    s.requote_enabled = None
    s.requote_max_attempts = None
    s.split_order_enabled = None
    s.split_order_threshold = None
    s.risk_timing = None
    data = serialize_strategy(s)
    assert data["requote_enabled"] is False
    assert data["requote_max_attempts"] == 2
    assert data["split_order_enabled"] is False
    assert data["split_order_threshold"] == 500000.0
    assert data["risk_timing"] == "next_day"
    db.close()


def test_service_create_update_roundtrip_execution_config():
    db = _db()
    pool_id, formula_id = _seed(db)
    req = StrategyCreate(
        requote_enabled=True, requote_max_attempts=3,
        split_order_enabled=True, split_order_threshold=800000.0,
        risk_timing="immediate", **_strategy_base(formula_id),
    )
    data = create_strategy(db, pool_id, req)
    sid = data["id"]
    assert data["requote_enabled"] is True
    assert data["requote_max_attempts"] == 3
    assert data["split_order_threshold"] == 800000.0
    assert data["risk_timing"] == "immediate"

    upd = StrategyCreate(**_strategy_base(formula_id))  # 全默认
    data2 = update_strategy(db, pool_id, sid, upd)
    assert data2["requote_enabled"] is False
    assert data2["requote_max_attempts"] == 2
    assert data2["split_order_enabled"] is False
    assert data2["split_order_threshold"] == 500000.0
    assert data2["risk_timing"] == "next_day"
    db.close()


# ---------------------------------------------------------------------------
# StrategyContext 默认 + builder 透传
# ---------------------------------------------------------------------------
def test_strategy_context_execution_config_defaults():
    ctx = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5,
    )
    assert ctx.requote_enabled is False
    assert ctx.requote_max_attempts == 2
    assert ctx.split_order_enabled is False
    assert ctx.split_order_threshold == Decimal("500000")
    assert ctx.risk_timing == "next_day"


def test_builder_passes_execution_config_to_context():
    db = _db()
    pool_id, formula_id = _seed(db)
    ps = PortfolioStrategy(
        name="ps", stock_pool_id=pool_id, initial_capital=Decimal("100000"),
    )
    db.add(ps)
    db.flush()
    s = Strategy(
        portfolio_id=ps.id, name="S", formula_id=formula_id,
        period="1d", role="independent",
        requote_enabled=True, requote_max_attempts=5,
        split_order_enabled=True, split_order_threshold=123456.0,
        risk_timing="immediate",
    )
    db.add(s)
    db.commit()
    port = assemble_portfolio(ps, [s], db)
    ctx = port.strategies[0]
    assert ctx.requote_enabled is True
    assert ctx.requote_max_attempts == 5
    assert ctx.split_order_enabled is True
    assert ctx.split_order_threshold == Decimal("123456")
    assert ctx.risk_timing == "immediate"
    db.close()
