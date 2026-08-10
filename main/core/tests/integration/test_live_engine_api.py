"""LiveEngine API 集成测试（0009 切片4）— TestClient + 内存 SQLite + Mock 桥。

验证 /api/live/sessions/{id}/start 接 LiveEngine（组装 portfolios + dispatcher + bar_poller
→ recover → start），/stop 停引擎，registry 按状态切换。
"""
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from core.main import app
from core.db import get_db
from core.models import (
    Base, StockPool, StockPoolStock, Formula, FormulaSignal,
    PortfolioStrategy, Strategy,
)
import core.api.live as live_api


@pytest.fixture
def client(tmp_path):
    """内存 SQLite（StaticPool 共享单连接）+ TestClient，覆盖 get_db。"""
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
    # 让 live API 的 db_session_factory 与 recover 用的 db 都指向测试库
    live_api.SessionLocal = Session
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, Session
    app.dependency_overrides.clear()
    # 清理注册表 + 还原 SessionLocal
    live_api._ENGINES.clear()
    import core.db as db_mod
    live_api.SessionLocal = db_mod.SessionLocal


def _seed(db):
    """最小依赖链：StockPool(+成分股) → Formula → FormulaSignal → PortfolioStrategy → Strategy。"""
    pool = StockPool(code="TEST", name="test_pool")
    db.add(pool)
    db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="600000.SH"))
    formula = Formula(name="open_formula", content="REF(CLOSE,1)")
    db.add(formula)
    db.flush()
    db.add(FormulaSignal(
        formula_id=formula.id, signal_name="open_sig",
        signal_type="OPEN", trigger_value=1,
    ))
    ps = PortfolioStrategy(
        name="test_portfolio", stock_pool_id=pool.id,
        initial_capital=Decimal("100000"),
        max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"),
    )
    db.add(ps)
    db.flush()
    db.add(Strategy(
        portfolio_id=ps.id, name="s1", formula_id=formula.id,
        period="1m", role="master",
        capital_ratio=Decimal("0.6"), max_positions=5,
        stop_loss_ratio=Decimal("0.05"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    ))
    db.commit()
    return ps.id


class _MockRecorder:
    """MockTransport：/ping、/order 返回成功，记录请求。"""

    def __init__(self):
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        path = request.url.path
        if path == "/order":
            return httpx.Response(200, json={"ok": True})
        if path == "/ping":
            return httpx.Response(200, json={"ok": True})
        if path == "/quote":
            return httpx.Response(200, json={"ok": True, "data": {}})
        return httpx.Response(404, json={"ok": False})


@pytest.fixture
def mock_bridge(monkeypatch):
    """把 live API 构造的 HttpBridgeDispatcher 替换为 MockTransport 版本。"""
    rec = _MockRecorder()

    real_cls = live_api.HttpBridgeDispatcher

    def fake_constructor(base_url="http://127.0.0.1:8790", token=None, **kw):
        client = httpx.Client(transport=httpx.MockTransport(rec.handler))
        return real_cls(base_url=base_url, token=token, client=client)

    monkeypatch.setattr(live_api, "HttpBridgeDispatcher", fake_constructor)
    return rec


def _create_session(c, name="live-test", portfolio_ids=(1,)):
    resp = c.post("/api/live/sessions", json={"name": name, "portfolio_ids": list(portfolio_ids)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    return body["data"]["id"]


def test_start_session_runs_engine(client, mock_bridge):
    """POST /start → session status=running，registry 有引擎实例。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid = _create_session(c, portfolio_ids=(ps_id,))
    resp = c.post("/api/live/sessions/%d/start" % sid)
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "running"
    assert sid in live_api._ENGINES

    # 停掉引擎，避免后台任务残留
    c.post("/api/live/sessions/%d/stop" % sid)


def test_stop_session_stops_engine(client, mock_bridge):
    """/stop → status=stopped，registry 清空引擎。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid = _create_session(c, portfolio_ids=(ps_id,))
    c.post("/api/live/sessions/%d/start" % sid)
    assert sid in live_api._ENGINES

    resp = c.post("/api/live/sessions/%d/stop" % sid)
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "stopped"
    assert sid not in live_api._ENGINES


def test_bridge_status_endpoint(client, mock_bridge):
    """未运行 → online=None；运行中 → online=heartbeat()。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid = _create_session(c, portfolio_ids=(ps_id,))
    # 未运行
    resp = c.get("/api/live/sessions/%d/bridge-status" % sid)
    assert resp.status_code == 200
    assert resp.json()["data"]["online"] is None
    assert resp.json()["data"]["status"] == "not_running"

    # 启动后桥在线（mock /ping 返回 ok）
    c.post("/api/live/sessions/%d/start" % sid)
    resp = c.get("/api/live/sessions/%d/bridge-status" % sid)
    assert resp.json()["data"]["online"] is True

    c.post("/api/live/sessions/%d/stop" % sid)


def test_get_session_includes_bridge_status(client, mock_bridge):
    """G7（0011 §5.11）：GET /sessions/{id} 并入 bridge_online/pending_orders/last_backfill_time。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid = _create_session(c, portfolio_ids=(ps_id,))
    # 未运行 → 三字段空值
    body = c.get("/api/live/sessions/%d" % sid).json()["data"]
    assert body["bridge_online"] is None
    assert body["pending_orders"] == 0
    assert body["last_backfill_time"] is None

    # 运行中 → bridge_online 实时心跳（mock /ping ok），pending/last_backfill 键存在
    c.post("/api/live/sessions/%d/start" % sid)
    body = c.get("/api/live/sessions/%d" % sid).json()["data"]
    assert body["bridge_online"] is True
    assert body["pending_orders"] == 0
    assert body["last_backfill_time"] is None

    c.post("/api/live/sessions/%d/stop" % sid)


def test_build_engine_fills_formula_mapping(client, mock_bridge):
    """_build_engine 后 LiveEngine 持有 _formula_by_strategy（strategy_id → formula_name）。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)  # 建 Formula(open_formula) + Strategy(formula_id)
    db.close()

    # 建 session + link（_build_engine 按 session_id 查 LiveSessionPortfolio）
    sid = _create_session(c, portfolio_ids=(ps_id,))

    # 直接调 _build_engine（不经 /start，避免起后台任务）
    db2 = Session()
    try:
        engine = live_api._build_engine(sid, db2)
    finally:
        db2.close()

    # 策略 1 的公式名 = open_formula（_seed 建的）
    assert 1 in engine._formula_by_strategy
    assert engine._formula_by_strategy[1] == "open_formula"
    # tq_formula 已注入
    assert engine._tq_formula is not None
    assert engine._formula_count == 200

