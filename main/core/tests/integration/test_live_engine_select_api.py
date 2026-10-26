"""实盘引擎选择 API 集成测试：session.engine 落库/回显 + 非法引擎拒绝。

用 StaticPool 共享单连接的内存 SQLite（TestClient 在独立线程访问同一库）。
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.main import app
from core.db import get_db
from core.models import Base


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
        yield c
    app.dependency_overrides.clear()
    live_api._ENGINES.clear()
    import core.db as db_mod
    live_api.SessionLocal = db_mod.SessionLocal


@pytest.mark.parametrize("engine_key", ["iquant", "tdx", "ths", "qmt"])
def test_create_and_list_session_engine(client, engine_key):
    r = client.post("/api/live/sessions",
                    json={"name": "s", "mode": "simulation", "engine": engine_key})
    body = r.json()
    assert body["code"] == 0
    assert body["data"]["engine"] == engine_key

    listed = client.get("/api/live/sessions").json()["data"]
    assert listed[0]["engine"] == engine_key


def test_create_session_rejects_unknown_engine(client):
    r = client.post("/api/live/sessions", json={"name": "s", "engine": "foo"})
    body = r.json()
    assert body["code"] == 400
    assert "engine" in body["message"]


def test_channel_endpoint_accepts_engine_query(client):
    body = client.get("/api/live/channel", params={"engine": "tdx"}).json()
    assert body["code"] == 0
    assert body["data"]["engine"] == "tdx"
    assert body["data"]["active"] == "tdx_tq"
    assert [e["key"] for e in body["data"]["engines"]] == ["iquant", "tdx", "ths", "qmt"]


def test_channel_test_rejects_unknown_channel(client):
    body = client.post("/api/live/channel/test", json={"channel": "foo"}).json()
    assert body["code"] == 400


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

    body = client.post(
        "/api/live/sessions",
        json={"name": "tdx", "mode": "simulation", "engine": "tdx",
              "execution_mode": "auto"},
    ).json()
    sid = body["data"]["id"]
    resp = client.post("/api/live/sessions/%d/start" % sid).json()
    assert resp["code"] == 503
    assert "通达信交易未就绪" in resp["message"]
    assert "资金账户未登录" in resp["message"]
    assert sid not in live_api._ENGINES
    assert client.get("/api/live/sessions/%d" % sid).json()["data"]["status"] != "running"


def test_start_qmt_session_reports_not_ready(client, monkeypatch):
    from core.services import live_service as svc

    class OfflineQmt:
        channel = "qmt"
        last_error = "未找到 xtquant（QMT 交易依赖）"

        def __init__(self, **kw):
            pass

        def heartbeat(self):
            return False

        def query_calendar(self):
            return []

        def close(self):
            pass

    monkeypatch.setattr(svc, "QmtDispatcher", OfflineQmt)

    sid = client.post(
        "/api/live/sessions",
        json={"name": "qmt", "mode": "live", "engine": "qmt",
              "execution_mode": "auto"},
    ).json()["data"]["id"]
    resp = client.post("/api/live/sessions/%d/start" % sid).json()
    assert resp["code"] == 503
    assert "QMT 交易未就绪" in resp["message"]
    assert "xtquant" in resp["message"]
