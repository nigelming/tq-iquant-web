from datetime import datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.db import get_db
from core.main import app
from core.models import (
    Base, Formula, LiveSession, LiveSignal, PortfolioStrategy, StockPool, Strategy,
)


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override
    with TestClient(app) as c:
        yield c, Session
    app.dependency_overrides.clear()


def _seed(Session):
    db = Session()
    db.add(LiveSession(id=1, name="s", mode="simulation", status="running",
                       engine="tdx", execution_mode="alert"))
    pool = StockPool(code="P", name="p")
    db.add(pool)
    db.flush()
    db.add(Formula(id=1, name="momentum", content="C"))
    port = PortfolioStrategy(id=1, name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    st = Strategy(id=1, portfolio_id=1, name="s1", formula_id=1,
                  period="1d", role="master")
    db.add_all([port, st])
    db.flush()
    db.add(LiveSignal(
        live_session_id=1, portfolio_strategy_id=1, strategy_id=1,
        stock_code="000001.SZ", period="1d", bar_time=datetime(2026, 10, 23, 15),
        formula_name="momentum", signal_name="buy", signal_type="OPEN",
        trigger_value=1, price=Decimal("12.34"), execution_mode="alert",
        status="alerted", alert_status="sent", tdx_run_id="7",
    ))
    db.commit()
    db.close()


def test_get_signals_filters_and_paginates(client):
    c, Session = client
    _seed(Session)
    resp = c.get("/api/live/sessions/1/signals?signal_type=OPEN&limit=10")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["total"] == 1
    item = body["data"]["items"][0]
    assert item["stock_code"] == "000001.SZ"
    assert Decimal(item["price"]) == Decimal("12.34")
    assert item["alert_status"] == "sent"


def test_get_signals_unknown_session_404(client):
    c, _ = client
    assert c.get("/api/live/sessions/99/signals").json()["code"] == 404
