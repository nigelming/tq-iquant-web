"""assemble_portfolio 跨池防御告警单测。

配置接口已对 "slave 池 ⊄ master 池" 硬拒绝 400；此处验证组装层对历史脏数据
仍会打 warning（不 raise，避免中断回测/实盘），让运行时可见。
"""
import logging
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.models import (
    Base, PortfolioStrategy, Strategy, StockPool, StockPoolStock, Formula,
)
from core.engine.portfolio_builder import assemble_portfolio


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _pool(db, codes):
    p = StockPool(code="P_" + "_".join(sorted(codes)), name="p")
    db.add(p); db.flush()
    for c in codes:
        db.add(StockPoolStock(pool_id=p.id, stock_code=c))
    db.commit()
    return p.id


def _build(db, master_codes, slave_codes):
    f = Formula(name="F", content="REF(CLOSE,1)")
    db.add(f); db.flush()
    master_pool = _pool(db, master_codes)
    slave_pool = _pool(db, slave_codes)
    ps = PortfolioStrategy(
        name="ps", stock_pool_id=master_pool, initial_capital=Decimal("100000"),
        max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"),
    )
    db.add(ps); db.flush()
    master = Strategy(
        portfolio_id=ps.id, name="M", formula_id=f.id, period="1d",
        role="master", stock_pool_id=master_pool,
    )
    db.add(master); db.flush()
    slave = Strategy(
        portfolio_id=ps.id, name="S", formula_id=f.id, period="1d", role="slave",
        master_strategy_id=master.id, stock_pool_id=slave_pool,
        slave_exit_mode="follow_master",
    )
    db.add(slave); db.commit()
    return ps, [master, slave]


def test_assemble_warns_when_slave_pool_exceeds_master(db, caplog):
    ps, strategies = _build(db, ["000001.SZ"], ["000001.SZ", "600000.SH"])

    with caplog.at_level(logging.WARNING, logger="core.engine.portfolio_builder"):
        assemble_portfolio(ps, strategies, db)

    assert any("越出 master" in r.getMessage() for r in caplog.records)
    assert any("600000.SH" in r.getMessage() for r in caplog.records)


def test_assemble_no_warning_when_slave_pool_subset(db, caplog):
    ps, strategies = _build(db, ["000001.SZ", "600000.SH"], ["000001.SZ"])

    with caplog.at_level(logging.WARNING, logger="core.engine.portfolio_builder"):
        assemble_portfolio(ps, strategies, db)

    assert not any("越出 master" in r.getMessage() for r in caplog.records)
