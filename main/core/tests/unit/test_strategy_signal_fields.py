from decimal import Decimal

from core.api.strategies import StrategyCreate, _validate_strategy
from core.engine.strategy_context import StrategyContext
from core.models import Base, Formula, PortfolioStrategy, Strategy, StockPool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_context_carries_signal_schedule_fields():
    ctx = StrategyContext(
        strategy_id=1, period="1d", capital_ratio=Decimal("0.5"),
        max_positions=3, signal_frequency="1w", signal_at="09:00",
        signal_weekday=1, signal_data_end="completed",
    )
    assert ctx.signal_frequency == "1w"
    assert ctx.signal_data_end == "completed"


def test_api_rejects_finer_frequency_and_current_week():
    db = _db()
    pool = StockPool(code="P", name="p")
    db.add(pool)
    db.flush()
    port = PortfolioStrategy(name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    f = Formula(name="f", content="C")
    db.add_all([port, f])
    db.commit()
    base = dict(name="s", formula_id=f.id, role="master",
                capital_ratio=0.5, max_positions=3)
    bad = StrategyCreate(period="1d", signal_frequency="1m", **base)
    assert "不能比 period 更细" in _validate_strategy(bad, db, port.id)
    bad2 = StrategyCreate(period="1d", signal_frequency="1w",
                          signal_at="09:00", signal_weekday=1,
                          signal_data_end="current", **base)
    assert "completed" in _validate_strategy(bad2, db, port.id)
    ok = StrategyCreate(period="1d", signal_frequency="1w",
                        signal_at="09:00", signal_weekday=1,
                        signal_data_end="completed", **base)
    assert _validate_strategy(ok, db, port.id) is None
