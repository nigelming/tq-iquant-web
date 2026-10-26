from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.models import (
    Base, Formula, LiveSession, LiveSignal, PortfolioStrategy, Strategy, StockPool,
)


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_live_signal_defaults_and_strategy_schedule_fields():
    db = _db()
    session = LiveSession(name="s", mode="simulation", status="stopped",
                          engine="tdx", execution_mode="alert")
    pool = StockPool(code="P", name="p")
    db.add_all([session, pool])
    db.flush()
    formula = Formula(name="momentum", content="C")
    db.add(formula)
    db.flush()
    port = PortfolioStrategy(name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    db.add(port)
    db.flush()
    strat = Strategy(portfolio_id=port.id, name="s1", formula_id=formula.id,
                     period="1d", role="master", signal_frequency="1w",
                     signal_at="09:00", signal_weekday=1,
                     signal_data_end="completed")
    db.add(strat)
    db.flush()
    row = LiveSignal(
        live_session_id=session.id, portfolio_strategy_id=port.id,
        strategy_id=strat.id, stock_code="000001.SZ", period="1d",
        bar_time=datetime(2026, 10, 23, 15, 0),
        formula_name="momentum", signal_name="buy",
        signal_type="OPEN", trigger_value=1, price=Decimal("12.34"),
        execution_mode="alert",
    )
    db.add(row)
    db.commit()
    assert row.status == "new"
    assert row.alert_status is None
    assert strat.signal_frequency == "1w"
    assert strat.signal_weekday == 1
    assert session.execution_mode == "alert"


def test_live_signal_unique_key_rejects_duplicate():
    import pytest
    from sqlalchemy.exc import IntegrityError

    db = _db()
    session = LiveSession(name="s", mode="simulation", status="stopped",
                          engine="tdx", execution_mode="alert")
    pool = StockPool(code="P", name="p")
    db.add_all([session, pool])
    db.flush()
    formula = Formula(name="momentum", content="C")
    db.add(formula)
    db.flush()
    port = PortfolioStrategy(name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    db.add(port)
    db.flush()
    strat = Strategy(portfolio_id=port.id, name="s1", formula_id=formula.id,
                     period="1d", role="master")
    db.add(strat)
    db.flush()
    kwargs = dict(
        live_session_id=session.id, portfolio_strategy_id=port.id,
        strategy_id=strat.id, stock_code="000001.SZ", period="1d",
        bar_time=datetime(2026, 10, 23, 15, 0), formula_name="f",
        signal_name="buy", signal_type="OPEN", trigger_value=1,
        price=Decimal("12.34"), execution_mode="alert",
    )
    db.add(LiveSignal(**kwargs))
    db.commit()
    db.add(LiveSignal(**kwargs))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()
