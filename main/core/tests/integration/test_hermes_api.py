"""hermes 对外信号接口测试（0017）— TestClient + 内存 SQLite。

GET /api/hermes/signals 跨 session 按日聚合 live_signals（无鉴权，局域网），
GET /api/hermes/health 探活。
"""
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
from core.services import hermes_service


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
    db.add(LiveSession(id=1, name="alpha", mode="simulation", status="running",
                       engine="tdx", execution_mode="alert"))
    db.add(LiveSession(id=2, name="beta", mode="simulation", status="stopped",
                       engine="tdx", execution_mode="alert"))
    pool = StockPool(code="P", name="p")
    db.add(pool)
    db.flush()
    db.add(Formula(id=1, name="momentum", content="C"))
    port = PortfolioStrategy(id=1, name="p", stock_pool_id=pool.id,
                             initial_capital=Decimal("100000"))
    st1 = Strategy(id=1, portfolio_id=1, name="s1", formula_id=1,
                   period="1d", role="master")
    st2 = Strategy(id=2, portfolio_id=1, name="s2", formula_id=1,
                   period="1d", role="master")
    db.add_all([port, st1, st2])
    db.flush()
    db.add(LiveSignal(
        live_session_id=1, portfolio_strategy_id=1, strategy_id=1,
        stock_code="000001.SZ", period="1d",
        bar_time=datetime(2026, 10, 26, 14, 20),
        formula_name="momentum", signal_name="buy", signal_type="OPEN",
        trigger_value=1, price=Decimal("12.34"), execution_mode="alert",
        status="new",
    ))
    db.add(LiveSignal(
        live_session_id=2, portfolio_strategy_id=1, strategy_id=2,
        stock_code="600000.SH", period="1d",
        bar_time=datetime(2026, 10, 26, 14, 20),
        formula_name="momentum", signal_name="sell", signal_type="CLOSE",
        trigger_value=1, price=Decimal("8.90"), execution_mode="alert",
        status="new",
    ))
    db.add(LiveSignal(
        live_session_id=1, portfolio_strategy_id=1, strategy_id=1,
        stock_code="000001.SZ", period="1d",
        bar_time=datetime(2026, 10, 23, 14, 20),
        formula_name="momentum", signal_name="buy", signal_type="OPEN",
        trigger_value=1, price=Decimal("12.10"), execution_mode="alert",
        status="alerted",
    ))
    db.commit()
    db.close()


def test_health(client):
    c, _ = client
    resp = c.get("/api/hermes/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["ok"] is True
    assert "date" in body["data"]


def test_signals_by_date_across_sessions(client):
    c, Session = client
    _seed(Session)
    resp = c.get("/api/hermes/signals?date=2026-10-26")
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["date"] == "2026-10-26"
    assert body["data"]["total"] == 2
    codes = {i["stock_code"] for i in body["data"]["items"]}
    assert codes == {"000001.SZ", "600000.SH"}
    item = next(i for i in body["data"]["items"] if i["stock_code"] == "000001.SZ")
    assert item["session_name"] == "alpha"
    assert item["session_id"] == 1
    assert item["signal_type"] == "OPEN"
    assert item["status"] == "new"
    assert Decimal(item["price"]) == Decimal("12.34")
    assert item["bar_time"].startswith("2026-10-26T14:20")


def test_signals_date_isolates_days(client):
    c, Session = client
    _seed(Session)
    resp = c.get("/api/hermes/signals?date=2026-10-23")
    body = resp.json()
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["status"] == "alerted"


def test_signals_default_today(client, monkeypatch):
    c, Session = client
    _seed(Session)
    monkeypatch.setattr(
        hermes_service, "now_shanghai",
        lambda: datetime(2026, 10, 26, 15, 0),
    )
    body = c.get("/api/hermes/signals").json()
    assert body["data"]["date"] == "2026-10-26"
    assert body["data"]["total"] == 2


def test_signals_filters(client):
    c, Session = client
    _seed(Session)
    resp = c.get("/api/hermes/signals?date=2026-10-26&strategy_id=2")
    assert resp.json()["data"]["total"] == 1
    resp = c.get("/api/hermes/signals?date=2026-10-26&portfolio_id=1")
    assert resp.json()["data"]["total"] == 2
    resp = c.get("/api/hermes/signals?date=2026-10-26&signal_type=CLOSE")
    assert resp.json()["data"]["total"] == 1
    resp = c.get("/api/hermes/signals?date=2026-10-26&portfolio_id=99")
    assert resp.json()["data"]["total"] == 0


def test_signals_empty_day_is_zero_not_404(client):
    c, _ = client
    body = c.get("/api/hermes/signals?date=2026-01-01").json()
    assert body["code"] == 0
    assert body["data"]["total"] == 0
    assert body["data"]["items"] == []


def test_signals_invalid_date_400(client):
    c, _ = client
    body = c.get("/api/hermes/signals?date=2026/10/26").json()
    assert body["code"] == 400
