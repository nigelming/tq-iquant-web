from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.models import (
    Base, Formula, FormulaSignal, LiveSession, LiveSessionPortfolio,
    PortfolioStrategy, StockPool, StockPoolStock, Strategy,
)
from core.services import live_service


def _db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _seed(db, execution_mode="alert"):
    db.add(LiveSession(id=1, name="s", mode="simulation", status="stopped",
                       engine="tdx", execution_mode=execution_mode))
    pool = StockPool(code="P", name="p")
    db.add(pool)
    db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="000001.SZ"))
    f = Formula(id=1, name="momentum", content="C")
    db.add(f)
    db.flush()
    db.add(FormulaSignal(formula_id=1, signal_name="buy",
                         signal_type="OPEN", trigger_value=1))
    port = PortfolioStrategy(id=1, name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    db.add(port)
    db.add(LiveSessionPortfolio(session_id=1, portfolio_strategy_id=1))
    db.add(Strategy(id=1, portfolio_id=1, name="s1", formula_id=1,
                    period="1d", role="master", signal_frequency="1w",
                    signal_at="09:00", signal_weekday=1,
                    signal_data_end="completed"))
    db.commit()


def test_create_session_defaults_to_alert():
    db = _db()
    row = live_service.create_session(db, {"name": "s", "engine": "tdx"}, "simulation")
    assert row["execution_mode"] == "alert"


def test_preflight_rejects_builtin():
    db = _db()
    _seed(db)
    db.query(Formula).filter_by(id=1).update({"engine": "builtin"})
    db.commit()
    status, msg = live_service.preflight_signal_config(db, 1, "alert")
    assert status == 503
    assert "通达信导入公式" in msg


def test_preflight_rejects_auto_custom_frequency():
    db = _db()
    _seed(db)
    status, msg = live_service.preflight_signal_config(db, 1, "auto")
    assert status == 503
    assert "P2" in msg


def test_preflight_alert_passes_with_fake_tq(monkeypatch):
    db = _db()
    _seed(db)
    monkeypatch.setattr(live_service, "_preflight_tq_alert", lambda: None)
    monkeypatch.setattr(live_service, "_smoke_formula_outputs",
                        lambda *a, **kw: None)
    assert live_service.preflight_signal_config(db, 1, "alert") is None
