from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.engine.event import BarEvent
from core.engine.live.signal_monitor import SignalMonitor
from core.models import (
    Base, Formula, FormulaSignal, LiveSession, LiveSignal, PortfolioStrategy,
    StockPool, StockPoolStock, Strategy,
)
from tq_iquant_shared.constants import SignalType


class FakeAlert:
    def __init__(self, ok=True):
        self.ok = ok
        self.items = []

    def send_warn(self, items):
        self.items.extend(items)
        if self.ok:
            return {"ok": True, "ErrorId": "0", "run_id": "7"}
        return {"ok": False, "ErrorId": "9", "Error": "boom", "run_id": ""}

    def send_message(self, msg):
        return {"ok": True}


def _factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine), engine


def _seed(Session):
    db = Session()
    db.add(LiveSession(id=1, name="s", mode="simulation", status="running",
                       engine="tdx", execution_mode="alert"))
    pool = StockPool(code="P", name="p")
    db.add(pool)
    db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="000001.SZ"))
    formula = Formula(id=10, name="momentum", content="C")
    db.add(formula)
    db.flush()
    db.add(FormulaSignal(formula_id=10, signal_name="buy",
                         signal_type="OPEN", trigger_value=1))
    port = PortfolioStrategy(id=1, name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    db.add(port)
    db.flush()
    st = Strategy(id=1, portfolio_id=1, name="s1", formula_id=10,
                  period="1d", role="master", signal_frequency="1d")
    db.add(st)
    db.commit()
    db.close()


def _ctx():
    st = SimpleNamespace(
        strategy_id=1, period="1d", signal_frequency="1d",
        formula_name="momentum", formula_id=10,
        formula_signals=[{"signal_name": "buy", "signal_type": SignalType.OPEN,
                          "trigger_value": 1}],
        stock_pool={"000001.SZ"},
    )
    return SimpleNamespace(portfolio_id=1, strategies=[st])


def _bar():
    return BarEvent(
        stocks={"000001.SZ": {"close": Decimal("12.34")}},
        bar_time=datetime(2026, 10, 26, 14, 30), period="1d",
    )


def _bar_at(t):
    return BarEvent(
        stocks={"000001.SZ": {"close": Decimal("12.34")}},
        bar_time=t, period="1d",
    )


def test_monitor_persists_and_alerts(monkeypatch):
    Session, _ = _factory()
    _seed(Session)
    alert = FakeAlert()
    events = []
    mon = SignalMonitor(
        1, Session, alert, lambda t, p: events.append((t, p)),
        "alert", clock=lambda: datetime(2026, 10, 26, 14, 30),
    )
    cache = {(1, "000001.SZ", datetime(2026, 10, 26, 14, 30)): [
        {"name": "buy", "value": 1},
    ]}
    mon.evaluate(_ctx(), _bar(), cache,
                 strategies=_ctx().strategies, frequency="1d")
    db = Session()
    row = db.query(LiveSignal).one()
    assert row.status == "alerted"
    assert row.alert_status == "sent"
    assert row.tdx_run_id == "7"
    assert alert.items[0].bs_flag == "0"
    assert "momentum-buy" == alert.items[0].reason
    assert events[0][0] == "signal"
    db.close()


def test_monitor_level_edge_and_dedup():
    Session, _ = _factory()
    _seed(Session)
    alert = FakeAlert()
    mon = SignalMonitor(1, Session, alert, lambda t, p: None, "alert")
    cache = {(1, "000001.SZ", datetime(2026, 10, 26, 14, 30)): [
        {"name": "buy", "value": 1},
    ]}
    ctx = _ctx()
    mon.evaluate(ctx, _bar(), cache, strategies=ctx.strategies, frequency="1d")
    mon.evaluate(ctx, _bar(), cache, strategies=ctx.strategies, frequency="1d")
    db = Session()
    assert db.query(LiveSignal).count() == 1
    db.close()


def test_monitor_marks_failed_alert():
    Session, _ = _factory()
    _seed(Session)
    mon = SignalMonitor(1, Session, FakeAlert(ok=False), lambda t, p: None, "alert")
    cache = {(1, "000001.SZ", datetime(2026, 10, 26, 14, 30)): [
        {"name": "buy", "value": 1},
    ]}
    ctx = _ctx()
    mon.evaluate(ctx, _bar(), cache, strategies=ctx.strategies, frequency="1d")
    db = Session()
    row = db.query(LiveSignal).one()
    assert row.status == "failed"
    assert row.alert_status == "failed"
    assert "boom" in row.alert_error
    db.close()


def test_monitor_failed_alert_rolls_back_last_value():
    Session, _ = _factory()
    _seed(Session)
    alert = FakeAlert(ok=False)
    mon = SignalMonitor(1, Session, alert, lambda t, p: None, "alert")
    ctx = _ctx()
    t1 = datetime(2026, 10, 26, 14, 30)
    cache1 = {(1, "000001.SZ", t1): [{"name": "buy", "value": 1}]}
    mon.evaluate(ctx, _bar_at(t1), cache1,
                 strategies=ctx.strategies, frequency="1d")
    key = (1, "000001.SZ", "buy")
    assert mon._last_value.get(key) is None
    t2 = datetime(2026, 10, 27, 14, 30)
    cache2 = {(1, "000001.SZ", t2): [{"name": "buy", "value": 1}]}
    mon.evaluate(ctx, _bar_at(t2), cache2,
                 strategies=ctx.strategies, frequency="1d")
    assert len(alert.items) == 2
    db = Session()
    assert db.query(LiveSignal).count() == 2
    db.close()


def test_monitor_success_keeps_last_value():
    Session, _ = _factory()
    _seed(Session)
    alert = FakeAlert(ok=True)
    mon = SignalMonitor(1, Session, alert, lambda t, p: None, "alert")
    ctx = _ctx()
    t1 = datetime(2026, 10, 26, 14, 30)
    cache = {(1, "000001.SZ", t1): [{"name": "buy", "value": 1}]}
    mon.evaluate(ctx, _bar_at(t1), cache,
                 strategies=ctx.strategies, frequency="1d")
    key = (1, "000001.SZ", "buy")
    assert mon._last_value[key] == 1
    mon.evaluate(ctx, _bar_at(t1), cache,
                 strategies=ctx.strategies, frequency="1d")
    assert len(alert.items) == 1
    db = Session()
    assert db.query(LiveSignal).count() == 1
    db.close()
