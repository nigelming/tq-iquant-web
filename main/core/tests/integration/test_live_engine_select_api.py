"""实盘引擎选择 API 集成测试：session.engine 落库/回显 + 非法引擎拒绝。

用 StaticPool 共享单连接的内存 SQLite（TestClient 在独立线程访问同一库）。
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.main import app
from core.db import get_db
from core.models import (
    Base, PortfolioStrategy, StockPool, StockPoolStock,
)


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    from core.api import live as live_api
    live_api.SessionLocal = Session
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, Session
    app.dependency_overrides.clear()
    live_api._ENGINES.clear()
    import core.db as db_mod
    live_api.SessionLocal = db_mod.SessionLocal


def _seed_pool(db):
    """最小组合依赖：非空股票池 → PortfolioStrategy（空池会被启动预检拦截）。"""
    pool = StockPool(code="TEST", name="test_pool")
    db.add(pool)
    db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="600000.SH"))
    ps = PortfolioStrategy(
        name="test_portfolio", stock_pool_id=pool.id,
        initial_capital=Decimal("100000"),
        max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"),
    )
    db.add(ps)
    db.commit()
    return ps.id


@pytest.mark.parametrize("engine_key", ["goldminer", "tdx", "ths", "iquant"])
def test_create_and_list_session_engine(client, engine_key):
    c, _ = client
    r = c.post("/api/live/sessions",
               json={"name": "s", "mode": "simulation", "engine": engine_key})
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["engine"] == engine_key

    listed = c.get("/api/live/sessions").json()["data"]
    assert listed[0]["engine"] == engine_key


def test_create_session_rejects_unknown_engine(client):
    c, _ = client
    r = c.post("/api/live/sessions", json={"name": "s", "engine": "foo"})
    body = r.json()
    assert body["code"] == 400
    assert "engine" in body["message"]


def test_channel_endpoint_accepts_engine_query(client):
    c, _ = client
    body = c.get("/api/live/channel", params={"engine": "tdx"}).json()
    assert body["code"] == 0
    assert body["data"]["engine"] == "tdx"
    assert body["data"]["active"] == "tdx_tq"
    assert [e["key"] for e in body["data"]["engines"]] == [
        "goldminer", "tdx", "ths", "iquant"]


def test_channel_test_rejects_unknown_channel(client):
    c, _ = client
    body = c.post("/api/live/channel/test", json={"channel": "foo"}).json()
    assert body["code"] == 400


def test_channel_test_accepts_iquant_bridge(client, monkeypatch):
    """QMT 桥已恢复接入：/channel/test 不再被「已退役」拒单，按 mode 选桥址。"""
    from core.services import live_service as svc

    class FakeBridge:
        def __init__(self, **kw):
            self.base_url = kw.get("base_url")

        def heartbeat(self):
            return True

    monkeypatch.setattr(svc, "HttpBridgeDispatcher", FakeBridge)
    c, _ = client
    body = c.post(
        "/api/live/channel/test",
        json={"channel": "iquant_bridge", "mode": "live"},
    ).json()
    assert body["code"] == 0
    assert body["data"]["ok"] is True
    assert body["data"]["channel"] == "iquant_bridge"
    # 桥址取自本机 config.yaml（live 腿端口随账户切换 8790/8791 变化），只验 mode 路由
    assert body["data"]["base_url"] == svc.iquant_config()["live"]


def test_start_iquant_session_probes_iquant_bridge(client, monkeypatch):
    """iquant 会话的 /start 经 make_dispatcher 构造桥；桥未就绪给 503 且提示可区分。"""
    from core.services import live_service as svc
    from core.api import live as live_api

    class OfflineBridge:
        channel = "iquant_bridge"
        last_probe_error = "refused"

        def __init__(self, **kw):
            pass

        def heartbeat(self):
            return False

        def query_calendar(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(svc, "HttpBridgeDispatcher", OfflineBridge)
    c, Session = client
    db = Session()
    ps_id = _seed_pool(db)
    db.close()
    body = c.post(
        "/api/live/sessions",
        json={"name": "iq", "mode": "simulation", "engine": "iquant",
              "execution_mode": "auto", "portfolio_ids": [ps_id]},
    ).json()
    sid = body["data"]["id"]
    resp = c.post("/api/live/sessions/%d/start" % sid).json()
    assert resp["code"] == 503
    assert "QMT 通道" in resp["message"]
    assert sid not in live_api._ENGINES


def test_start_tdx_session_reports_not_ready(client, monkeypatch):
    """通达信未登录/未启动时 /start 返回 503，且不置 running、不建引擎。"""
    from core.services import live_service as svc
    from core.api import live as live_api

    class OfflineTdx:
        channel = "tdx_tq"
        last_error = "获取交易账户句柄失败: 资金账户未登录或不存在。"

        def __init__(self, **kw):
            pass

        def heartbeat(self):
            return False

        def query_calendar(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(svc, "TdxTqDispatcher", OfflineTdx)

    c, Session = client
    db = Session()
    ps_id = _seed_pool(db)
    db.close()
    body = c.post(
        "/api/live/sessions",
        json={"name": "tdx", "mode": "simulation", "engine": "tdx",
              "execution_mode": "auto", "portfolio_ids": [ps_id]},
    ).json()
    sid = body["data"]["id"]
    resp = c.post("/api/live/sessions/%d/start" % sid).json()
    assert resp["code"] == 503
    assert "通达信交易未就绪" in resp["message"]
    assert "资金账户未登录" in resp["message"]
    assert sid not in live_api._ENGINES
    assert c.get("/api/live/sessions/%d" % sid).json()["data"]["status"] != "running"
