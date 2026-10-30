"""LiveEngine API 集成测试（0009 切片4）— TestClient + 内存 SQLite + Mock 桥。

验证 /api/live/sessions/{id}/start 接 LiveEngine（组装 portfolios + dispatcher + bar_poller
→ recover → start），/stop 停引擎，registry 按状态切换。
B4b：/orders、/trades、/positions 三个历史查询端点。
"""
from datetime import datetime
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from core.main import app
from core.db import get_db
from core.models import (
    Base, StockPool, StockPoolStock, Formula, FormulaSignal,
    PortfolioStrategy, Strategy, LiveOrder, LiveTrade, LiveSessionPortfolio,
)
from core.engine.position import Position
import core.api.live as live_api
from core.services import live_service as live_svc


@pytest.fixture
def client(tmp_path):
    """tmp 文件 SQLite（每会话独立连接）+ TestClient，覆盖 get_db。

    注：此前是内存 SQLite + StaticPool（所有 session 共享单条连接）——LiveEngine
    后台 tick 线程（_sweep_timeout_orders / persist_risk_state 等经 db_session_factory
    开会话）与 HTTP 请求线程会在同一连接上并发执行语句，互踩游标状态
    （sqlite3.InterfaceError: bad parameter or other API misuse / 事务交错），偶发
    幂等返回错乱。M1 多会话并行测试加重引擎启停密度后概率显著上升，故改为 tmp
    文件库：每会话独享连接，SQLite 文件锁 + busy timeout 天然串行化跨线程访问。
    """
    engine = create_engine(
        "sqlite:///%s" % (tmp_path / "test.db").as_posix(),
        connect_args={"check_same_thread": False, "timeout": 30},
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

    def fake_constructor(base_url="http://127.0.0.1:8790", **kw):
        client = httpx.Client(transport=httpx.MockTransport(rec.handler))
        return real_cls(base_url=base_url, client=client)

    monkeypatch.setattr(live_api, "HttpBridgeDispatcher", fake_constructor)
    return rec


def _create_session(c, name="live-test", portfolio_ids=(1,), mode="simulation",
                    execution_mode="auto", engine="iquant", live_capital=None):
    """本文件为 iQuant 桥测试（mock_bridge）——显式钉住 engine="iquant"，
    不依赖本地 config.yaml 的 tq_trade.default_engine（批次 A 后默认 tdx，
    落到 tdx 会走真实 TDX 预检/公式冒烟，桥 mock 完全失效）。"""
    payload = {
        "name": name, "portfolio_ids": list(portfolio_ids), "mode": mode,
        "execution_mode": execution_mode,
    }
    if engine:
        payload["engine"] = engine
    if live_capital is not None:
        payload["live_capital"] = live_capital
    resp = c.post("/api/live/sessions", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    return body["data"]["id"]


def test_create_session_with_multiple_portfolios(client, mock_bridge):
    """POST /sessions 带 portfolio_ids → LiveSessionPortfolio 落多行;list_sessions 返回 portfolio_ids。"""
    c, Session = client
    db = Session()
    ps1 = _seed(db)
    # 再建一个组合,拿第二个 portfolio id
    pool2 = StockPool(code="TEST2", name="test_pool2")
    db.add(pool2)
    db.flush()
    db.add(StockPoolStock(pool_id=pool2.id, stock_code="000001.SZ"))
    formula2 = Formula(name="open_formula2", content="REF(CLOSE,1)")
    db.add(formula2)
    db.flush()
    ps2 = PortfolioStrategy(
        name="test_portfolio2", stock_pool_id=pool2.id,
        initial_capital=Decimal("200000"),
        max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"),
    )
    db.add(ps2)
    db.commit()
    ps2_id = ps2.id
    db.close()

    sid = _create_session(c, portfolio_ids=(ps1, ps2_id))

    # list_sessions 返回两个组合 id
    resp = c.get("/api/live/sessions")
    row = next(s for s in resp.json()["data"] if s["id"] == sid)
    assert sorted(row["portfolio_ids"]) == sorted([ps1, ps2_id])

    # get_session 同样返回
    detail = c.get("/api/live/sessions/%d" % sid).json()["data"]
    assert {p["portfolio_id"] for p in detail["portfolios"]} == {ps1, ps2_id}

    # 引擎组装:两个组合都进了 portfolios
    c.post("/api/live/sessions/%d/start" % sid)
    try:
        engine = live_api._ENGINES[sid]
        assert len(engine.portfolios) == 2
    finally:
        c.post("/api/live/sessions/%d/stop" % sid)


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


def _seed_extra_portfolio(db):
    """追加一个独立组合（池+成分股），返回组合 id（供多会话并行测试用）。"""
    pool = StockPool(code="TEST2", name="test_pool2")
    db.add(pool)
    db.flush()
    db.add(StockPoolStock(pool_id=pool.id, stock_code="000001.SZ"))
    ps = PortfolioStrategy(
        name="test_portfolio2", stock_pool_id=pool.id,
        initial_capital=Decimal("200000"),
        max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"),
    )
    db.add(ps)
    db.commit()
    return ps.id


class _FakeThsDispatcher:
    """THS 通道替身：heartbeat 通过 + file_builtin（绕过 OCR 启动闸）+ 空行情。"""
    channel = "ths_ui"
    execute_mode = "file_builtin"
    supports_batch_quote = False

    def __init__(self, **kw):
        self.init_kwargs = kw

    def heartbeat(self):
        return True

    def query_quote(self, code, period="1m", count=10):
        return []

    def query_calendar(self):
        return []

    def query_positions(self):
        return []


class _FakeTdxDispatcher:
    """TDX 通道替身：heartbeat 通过 + 空行情（不触真实通达信）。"""
    channel = "tdx_tq"
    supports_batch_quote = False

    def __init__(self, **kw):
        pass

    def heartbeat(self):
        return True

    def query_quote(self, code, period="1m", count=10):
        return []

    def query_calendar(self):
        return []

    def query_positions(self):
        return []


def test_two_non_ths_sessions_run_in_parallel(client, mock_bridge):
    """M1 多会话并行（B6 改资源互斥）：两个非 ths 会话先后 start → 都注册运行。"""
    c, Session = client
    db = Session()
    ps1 = _seed(db)
    ps2 = _seed_extra_portfolio(db)
    db.close()

    sid1 = _create_session(c, portfolio_ids=(ps1,))
    sid2 = _create_session(c, portfolio_ids=(ps2,))

    assert c.post("/api/live/sessions/%d/start" % sid1).json()["data"]["status"] == "running"
    assert c.post("/api/live/sessions/%d/start" % sid2).json()["data"]["status"] == "running"
    assert sid1 in live_api._ENGINES
    assert sid2 in live_api._ENGINES

    c.post("/api/live/sessions/%d/stop" % sid1)
    c.post("/api/live/sessions/%d/stop" % sid2)


def test_overlapping_portfolio_sessions_start_allowed(client, mock_bridge):
    """M1 复制交易语义：同组合挂多个运行中会话 → 启动不拒（各会话独立虚拟账本）。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid1 = _create_session(c, portfolio_ids=(ps_id,))
    sid2 = _create_session(c, portfolio_ids=(ps_id,))

    assert c.post("/api/live/sessions/%d/start" % sid1).json()["code"] == 0
    resp = c.post("/api/live/sessions/%d/start" % sid2)
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["status"] == "running"
    assert sid2 in live_api._ENGINES

    c.post("/api/live/sessions/%d/stop" % sid1)
    c.post("/api/live/sessions/%d/stop" % sid2)


def test_ths_second_session_rejected_channel_exclusive(client, mock_bridge, monkeypatch):
    """M1 THS 键鼠独占：ths 会话运行中，第二个 ths 会话 start → 409 拒绝。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()
    monkeypatch.setattr(live_svc, "ThsUiDispatcher", _FakeThsDispatcher)

    sid1 = _create_session(c, portfolio_ids=(ps_id,), engine="ths")
    sid2 = _create_session(c, portfolio_ids=(ps_id,), engine="ths")

    assert c.post("/api/live/sessions/%d/start" % sid1).json()["code"] == 0
    assert sid1 in live_api._ENGINES

    resp = c.post("/api/live/sessions/%d/start" % sid2)
    assert resp.status_code == 200  # 统一响应格式，业务错误在 body.code
    assert resp.json()["code"] != 0
    assert "同花顺" in resp.json()["message"]
    assert sid2 not in live_api._ENGINES
    assert sid1 in live_api._ENGINES  # 原会话不受影响

    c.post("/api/live/sessions/%d/stop" % sid1)


def test_ths_running_tdx_session_start_allowed(client, mock_bridge, monkeypatch):
    """M1：ths 运行中 + tdx 会话启动 → 成功（互斥仅限 THS 通道自身）。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()
    monkeypatch.setattr(live_svc, "ThsUiDispatcher", _FakeThsDispatcher)
    monkeypatch.setattr(live_svc, "TdxTqDispatcher", _FakeTdxDispatcher)

    sid1 = _create_session(c, portfolio_ids=(ps_id,), engine="ths")
    sid2 = _create_session(c, portfolio_ids=(ps_id,), engine="tdx")

    assert c.post("/api/live/sessions/%d/start" % sid1).json()["code"] == 0
    resp = c.post("/api/live/sessions/%d/start" % sid2)
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["status"] == "running"
    assert sid1 in live_api._ENGINES
    assert sid2 in live_api._ENGINES

    c.post("/api/live/sessions/%d/stop" % sid1)
    c.post("/api/live/sessions/%d/stop" % sid2)


def test_restart_same_running_session_is_idempotent(client, mock_bridge):
    """B6：同一 session 重复 start → 幂等返回 running（非拒绝）。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid = _create_session(c, portfolio_ids=(ps_id,))
    c.post("/api/live/sessions/%d/start" % sid)
    resp = c.post("/api/live/sessions/%d/start" % sid)
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["status"] == "running"

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


def test_stop_session_is_idempotent(client, mock_bridge):
    """已停止的 session 再次 /stop → 仍返回 200（不是 404）。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid = _create_session(c, portfolio_ids=(ps_id,))
    c.post("/api/live/sessions/%d/start" % sid)
    assert c.post("/api/live/sessions/%d/stop" % sid).json()["code"] == 0

    resp = c.post("/api/live/sessions/%d/stop" % sid)
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["status"] == "stopped"


def test_create_session_rejects_duplicate_and_missing_portfolio(client, mock_bridge):
    """portfolio_ids 重复/不存在 → 400 业务错误，而不是 FK/唯一约束的 500。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    dup = c.post("/api/live/sessions",
                 json={"name": "dup", "portfolio_ids": [ps_id, ps_id]})
    assert dup.status_code == 200
    assert dup.json()["code"] != 0
    assert "重复" in dup.json()["message"]

    missing = c.post("/api/live/sessions",
                     json={"name": "missing", "portfolio_ids": [999999]})
    assert missing.status_code == 200
    assert missing.json()["code"] != 0
    assert "不存在" in missing.json()["message"]


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


def test_get_and_list_sessions_include_reconcile_mismatches(client, mock_bridge):
    """D-3（I-7）：session 详情/列表序列化并入 reconcile_mismatches——
    运行中取引擎内存最近一次对账差异摘要；未运行（无引擎）为 []。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid = _create_session(c, portfolio_ids=(ps_id,))
    # 未运行 → []
    body = c.get("/api/live/sessions/%d" % sid).json()["data"]
    assert body["reconcile_mismatches"] == []

    c.post("/api/live/sessions/%d/start" % sid)
    engine = live_api._ENGINES[sid]
    engine._reconcile_mismatches.append(
        {"code": "600000.SH", "virtual": 1000, "real": 800, "diff": 200}
    )
    # 详情 → 引擎内存差异
    body = c.get("/api/live/sessions/%d" % sid).json()["data"]
    assert body["reconcile_mismatches"] == [
        {"code": "600000.SH", "virtual": 1000, "real": 800, "diff": 200},
    ]
    # 列表 → 同样并入（service 层从注册表取）
    row = next(s for s in c.get("/api/live/sessions").json()["data"] if s["id"] == sid)
    assert row["reconcile_mismatches"] == [
        {"code": "600000.SH", "virtual": 1000, "real": 800, "diff": 200},
    ]

    c.post("/api/live/sessions/%d/stop" % sid)
    # 停止（引擎已弹出注册表）→ []
    body = c.get("/api/live/sessions/%d" % sid).json()["data"]
    assert body["reconcile_mismatches"] == []


def test_sse_line_formats_event():
    """B5：_sse_line 把 {type, **payload} 格式化为 SSE 行（data 去掉 type 字段）。"""
    line = live_api._sse_line({"type": "signal", "portfolio_id": 1, "strategy_id": 1})
    assert line == "event: signal\ndata: {\"portfolio_id\": 1, \"strategy_id\": 1}\n\n"
    ping = live_api._sse_line({"type": "ping", "time": "2026-08-05T14:30:00"})
    assert ping.startswith("event: ping\ndata: ")
    assert "\"time\"" in ping


def test_heartbeat_stream_yields_ping_when_idle():
    """B5：未运行 session 的心跳流（_heartbeat_stream）发 event: ping 保活。"""
    import asyncio

    class _FakeRequest:
        async def is_disconnected(self):
            return False

    async def drive():
        gen = live_api._heartbeat_stream(_FakeRequest(), 1)
        it = gen.__aiter__()
        first = await it.__anext__()
        assert first.startswith("event: ping\ndata: ")
        assert "\"time\"" in first
        await gen.aclose()

    asyncio.run(drive())


def test_heartbeat_stream_emits_reconnect_when_engine_registers():
    """D-5（前端 I4）：心跳流在引擎注册（session 启动）后发 event: reconnect 并结束——
    前端收到后关闭当前 EventSource 重连，落到真事件流。"""
    import asyncio

    class _FakeRequest:
        async def is_disconnected(self):
            return False

    async def drive():
        gen = live_api._heartbeat_stream(_FakeRequest(), 4242, ping_interval=0.01)
        it = gen.__aiter__()
        first = await it.__anext__()
        assert first.startswith("event: ping\ndata: ")
        # 引擎注册（值本身不重要，非 None 即视为运行中）
        live_api._ENGINES[4242] = object()
        try:
            second = await it.__anext__()
            assert second.startswith("event: reconnect\ndata: ")
            assert '"session_id": 4242' in second
            # 发完 reconnect 流即结束（客户端 EventSource 触发重连）
            with pytest.raises(StopAsyncIteration):
                await it.__anext__()
        finally:
            live_api._ENGINES.pop(4242, None)

    asyncio.run(drive())


def test_stream_endpoint_pings_when_not_running(client, mock_bridge, monkeypatch):
    """B5：未运行 session → /stream 走 _heartbeat_stream，返回 SSE 心跳。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()
    sid = _create_session(c, portfolio_ids=(ps_id,))

    async def fake_heartbeat(request, session_id):
        yield "event: ping\ndata: {\"time\": \"x\"}\n\n"
    monkeypatch.setattr(live_api, "_heartbeat_stream", fake_heartbeat)

    resp = c.get("/api/live/sessions/%d/stream" % sid)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: ping" in resp.text


def test_stream_endpoint_relays_engine_events_when_running(client, mock_bridge):
    """B5：运行中 session → /stream 转发引擎 stream_events（引擎事件 → SSE 行）。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()
    sid = _create_session(c, portfolio_ids=(ps_id,))
    c.post("/api/live/sessions/%d/start" % sid)
    engine = live_api._ENGINES[sid]

    async def fake_stream():
        yield {"type": "signal", "portfolio_id": 1, "strategy_id": 1, "signal_name": "open_sig"}
        yield {"type": "ping", "time": "2026-08-05T14:30:00"}
    engine.stream_events = fake_stream  # 实例属性遮蔽方法，端点消费该有限流

    resp = c.get("/api/live/sessions/%d/stream" % sid)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert "event: signal" in resp.text
    assert '"portfolio_id": 1' in resp.text
    assert '"signal_name": "open_sig"' in resp.text
    assert "event: ping" in resp.text

    c.post("/api/live/sessions/%d/stop" % sid)


def test_stream_endpoint_404_when_session_missing(client):
    """#13/#5：session 不存在 → HTTP 404（HTTPException pass-through，非 body-code）。

    EventSource 需真实 HTTP 错误码才触发 onerror，body-code+200 会让前端静默失败。
    全局 Exception 处理器不拦截 HTTPException，此行为保持。
    """
    c, _ = client
    resp = c.get("/api/live/sessions/9999/stream")
    assert resp.status_code == 404
    # HTTPException pass-through：响应体是 {"detail":...}，非统一 envelope
    assert "session" in resp.json()["detail"]


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
    # #27：formula_count 按公式配 → 引擎收到 {formula_name: count}（_seed 默认 200）
    assert engine._formula_count_by_name == {"open_formula": 200}


# ---- 0011 外部信号注入：signal_file_path → build_engine 接线 ----

def test_create_and_build_engine_wires_signal_file_source(client, mock_bridge):
    """POST /sessions 带 signal_file_path → 落库；_build_engine 建信号源
    （公式名集合 = formula_by_strategy 值集）；未配置会话 → _signal_source is None。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    resp = c.post("/api/live/sessions", json={
        "name": "ext", "portfolio_ids": [ps_id], "mode": "simulation",
        "execution_mode": "auto", "engine": "iquant",
        "signal_file_path": "D:/ths/alert.txt",
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    sid = body["data"]["id"]

    db2 = Session()
    try:
        engine = live_api._build_engine(sid, db2)
        assert engine._signal_source is not None
        assert engine._signal_source._formula_names == {"open_formula"}
    finally:
        db2.close()

    # 未配置 signal_file_path 的会话 → 不启用（引擎行为不变）
    sid2 = _create_session(c, name="no-ext", portfolio_ids=(ps_id,))
    db3 = Session()
    try:
        engine2 = live_api._build_engine(sid2, db3)
        assert engine2._signal_source is None
    finally:
        db3.close()


# ---- B4b: 历史查询端点（orders / trades / positions）----

def _add_order(db, sid, stock="600000.SH", status="filled", trade_type="BUY",
               qty=100, price="10.5", signal_name="open_sig", signal_type="OPEN",
               filled_qty=None):
    """插入一条 LiveOrder，返回实例（未 commit，由调用方控制）。"""
    return LiveOrder(
        live_session_id=sid, portfolio_strategy_id=1, strategy_id=1,
        stock_code=stock, trade_type=trade_type, order_type="LIMIT",
        price=Decimal(price), quantity=qty,
        filled_quantity=qty if filled_qty is None else filled_qty,
        filled_price=Decimal(price) if filled_qty is None else None,
        status=status, signal_name=signal_name, signal_type=signal_type,
        bar_time=datetime(2026, 8, 5, 10, 30),
    )


def _add_trade(db, sid, stock="600000.SH", trade_type="BUY", qty=100, price="10.5",
               trade_time=None, order=None):
    """插入一条 LiveTrade，返回实例（未 commit）。"""
    p = Decimal(price)
    return LiveTrade(
        live_session_id=sid,
        live_order_id=order.id if order else None,
        portfolio_strategy_id=1, strategy_id=1,
        stock_code=stock, trade_type=trade_type,
        price=p, quantity=qty, amount=p * qty,
        commission=Decimal("0.50"), stamp_duty=Decimal("0"),
        trade_time=trade_time or datetime(2026, 8, 5, 10, 31),
    )


def test_query_orders_history(client, mock_bridge):
    """GET /sessions/{id}/orders → 返回全部委托；?status= 过滤。"""
    c, Session = client
    sid = _create_session(c, portfolio_ids=())
    db = Session()
    db.add_all([
        _add_order(db, sid, status="filled"),
        _add_order(db, sid, status="submitted", trade_type="SELL", qty=200),
    ])
    db.commit()
    db.close()

    resp = c.get("/api/live/sessions/%d/orders" % sid)
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert len(body["data"]) == 2
    assert {o["status"] for o in body["data"]} == {"filled", "submitted"}
    # 同 created_at 时按 id 倒序 → 新单(submitted)在前
    assert body["data"][0]["status"] == "submitted"
    assert body["data"][1]["status"] == "filled"

    resp2 = c.get("/api/live/sessions/%d/orders?status=submitted" % sid)
    rows = resp2.json()["data"]
    assert len(rows) == 1
    assert rows[0]["trade_type"] == "SELL"

    # 会话不存在 → body code 404
    resp3 = c.get("/api/live/sessions/9999/orders")
    assert resp3.json()["code"] == 404


def test_query_orders_returns_empty_list_when_none(client, mock_bridge):
    """无任何委托 → data 为空列表（非 null）。"""
    c, Session = client
    sid = _create_session(c, portfolio_ids=())
    resp = c.get("/api/live/sessions/%d/orders" % sid)
    assert resp.json()["code"] == 0
    assert resp.json()["data"] == []


def test_query_trades_history(client, mock_bridge):
    """GET /sessions/{id}/trades → 成交明细按 trade_time 倒序。"""
    c, Session = client
    sid = _create_session(c, portfolio_ids=())
    db = Session()
    lo = _add_order(db, sid)
    db.add(lo)
    db.flush()
    db.add_all([
        _add_trade(db, sid, order=lo, trade_time=datetime(2026, 8, 5, 10, 31)),
        _add_trade(db, sid, trade_type="SELL", qty=100, price="11.0",
                   trade_time=datetime(2026, 8, 5, 10, 35)),
    ])
    db.commit()
    db.close()

    resp = c.get("/api/live/sessions/%d/trades" % sid)
    body = resp.json()
    assert body["code"] == 0
    assert len(body["data"]) == 2
    # 倒序：SELL(10:35) 在前
    assert body["data"][0]["trade_type"] == "SELL"
    assert body["data"][1]["trade_type"] == "BUY"
    assert body["data"][1]["price"] == 10.5
    assert body["data"][1]["amount"] == 1050.0


def test_query_positions_when_stopped_aggregates_from_trades(client, mock_bridge):
    """未运行 → /positions 从 live_trades 重放聚合（BUY 加、SELL 减，均价加权）。

    600@10 BUY + 200@12 BUY + 100@11 SELL → 净 700 股，均价 (600*10+200*12)/800=10.5。
    """
    c, Session = client
    sid = _create_session(c, portfolio_ids=())
    db = Session()
    db.add_all([
        _add_trade(db, sid, qty=600, price="10"),
        _add_trade(db, sid, qty=200, price="12"),
        _add_trade(db, sid, trade_type="SELL", qty=100, price="11"),
    ])
    db.commit()
    db.close()

    resp = c.get("/api/live/sessions/%d/positions" % sid)
    body = resp.json()
    assert body["code"] == 0
    row = next(p for p in body["data"] if p["stock_code"] == "600000.SH")
    assert row["quantity"] == 700
    assert row["avg_cost"] == 10.5
    assert row["market_value"] == pytest.approx(700 * 10.5)
    # 归属：_add_trade 恒为 portfolio_strategy_id=1/strategy_id=1
    assert row["portfolio_id"] == 1
    assert row["strategy_id"] == 1


def test_query_positions_splits_rows_by_strategy(client, mock_bridge):
    """同票被两个子策略持有 → 停止态重放按 (组合, 子策略) 拆成两行。"""
    c, Session = client
    sid = _create_session(c, portfolio_ids=())
    db = Session()
    tr1 = _add_trade(db, sid, qty=300, price="10")
    tr2 = _add_trade(db, sid, qty=200, price="12")
    tr2.strategy_id = 2  # 第二个子策略持有同一只票
    db.add_all([tr1, tr2])
    db.commit()
    db.close()

    resp = c.get("/api/live/sessions/%d/positions" % sid)
    body = resp.json()
    assert body["code"] == 0
    rows = [p for p in body["data"] if p["stock_code"] == "600000.SH"]
    assert len(rows) == 2
    by_sid = {r["strategy_id"]: r for r in rows}
    assert by_sid[1]["quantity"] == 300
    assert by_sid[2]["quantity"] == 200


def test_query_positions_uses_engine_when_running(client, mock_bridge):
    """运行中 → /positions 读引擎内存态虚拟持仓（含未落库的当日变动），带归属 id。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()
    sid = _create_session(c, portfolio_ids=(ps_id,))
    c.post("/api/live/sessions/%d/start" % sid)
    try:
        engine = live_api._ENGINES[sid]
        # 直接往引擎内存态塞一个持仓（模拟当日已成交未落库/或恢复态）
        pos = Position("600000.SH")
        pos.buy(600, Decimal("10.5"))
        engine.portfolios[0].strategies[0].positions["600000.SH"] = pos

        resp = c.get("/api/live/sessions/%d/positions" % sid)
        body = resp.json()
        assert body["code"] == 0
        row = next(p for p in body["data"] if p["stock_code"] == "600000.SH")
        assert row["quantity"] == 600
        assert row["avg_cost"] == 10.5
        # 归属 id 与引擎内存态一致（不硬编码，测试库里 id 未必为 1）
        assert row["portfolio_id"] == engine.portfolios[0].portfolio_id
        assert row["strategy_id"] == engine.portfolios[0].strategies[0].strategy_id
    finally:
        c.post("/api/live/sessions/%d/stop" % sid)


# ---- 双桥：_bridge_config 返回双址；_build_engine 按 session.mode 选桥 ----

def test_bridge_config_returns_dual_urls():
    """_bridge_config() 返回 simulation/live 两个 base_url（对齐 config.yaml 双桥结构）。"""
    br = live_api._bridge_config()
    assert br["simulation"] == "http://127.0.0.1:8790"
    assert br["live"] == "http://127.0.0.1:8791"


def test_build_engine_picks_bridge_by_mode(client, monkeypatch):
    """_build_engine(mode) 按 mode 选桥址：simulation→8790，live→8791，
    未知 mode 兜底仿真桥(8790，防误传走到真实资金桥)。

    simulation/live 用真实创建对应 mode 的 session；unknown mode 不会被
    create_session 接受（白名单），故用 simulation session 直接调 _build_engine
    传 unknown，验证兜底分支。
    """
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    captured = {}

    real_cls = live_api.HttpBridgeDispatcher

    def fake_constructor(base_url="http://127.0.0.1:8790", **kw):
        captured["base_url"] = base_url
        # 复用 mock_bridge 的 MockTransport 思路，给一个能 /ping 的 client
        client_ = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True})))
        return real_cls(base_url=base_url, client=client_)

    monkeypatch.setattr(live_api, "HttpBridgeDispatcher", fake_constructor)
    # 显式钉全局通道选择：本地 config.yaml ths_ui.enabled=true 时 _dispatcher_cls()
    # 会返回 ThsUiDispatcher，绕过上面的类属性 patch（消除对本地配置的隐性依赖）。
    monkeypatch.setattr(live_api, "_dispatcher_cls", lambda: fake_constructor)

    # simulation / live：创建对应 mode 的 session 后直接调 _build_engine
    # （显式钉 engine=iquant：消除对本地 tq_trade.default_engine 的隐性依赖）
    for mode, expected in [("simulation", "http://127.0.0.1:8790"),
                           ("live", "http://127.0.0.1:8791")]:
        sid = _create_session(c, portfolio_ids=(ps_id,), mode=mode, engine="iquant")
        db2 = Session()
        try:
            live_api._build_engine(sid, db2, mode)
        finally:
            db2.close()
        assert captured["base_url"] == expected, "mode=%s 应选 %s，实际 %s" % (mode, expected, captured["base_url"])

    # unknown：create_session 白名单会拒，用已存在的 simulation session 直接调 _build_engine
    # 传 unknown mode，验证兜底走仿真桥（防误传走到真实资金桥）。
    sim_sid = _create_session(c, portfolio_ids=(ps_id,), mode="simulation", engine="iquant")
    db3 = Session()
    try:
        live_api._build_engine(sim_sid, db3, "unknown")
    finally:
        db3.close()
    assert captured["base_url"] == "http://127.0.0.1:8790", "unknown mode 应兜底仿真桥，实际 %s" % captured["base_url"]


def test_start_session_routes_engine_to_live_bridge(client, monkeypatch):
    """start_session 传 session.mode 给 _build_engine → 实盘 session 走 8791 桥。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    captured = {}
    real_cls = live_api.HttpBridgeDispatcher

    def fake_constructor(base_url="http://127.0.0.1:8790", **kw):
        captured["base_url"] = base_url
        client_ = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True})))
        return real_cls(base_url=base_url, client=client_)

    monkeypatch.setattr(live_api, "HttpBridgeDispatcher", fake_constructor)

    # A-6 后全局默认引擎为 tq_trade.default_engine（tdx）；本测试验证 iQuant 桥按
    # mode 选桥址，显式指定 engine=iquant 不依赖全局默认。
    sid = _create_session(c, portfolio_ids=(ps_id,), mode="live", engine="iquant")
    resp = c.post("/api/live/sessions/%d/start" % sid)
    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    assert captured["base_url"] == "http://127.0.0.1:8791"
    c.post("/api/live/sessions/%d/stop" % sid)


def test_create_session_rejects_invalid_mode(client, mock_bridge):
    """create_session 对非法 mode 返回业务错误（白名单 simulation|live）。"""
    c, _ = client
    resp = c.post("/api/live/sessions", json={"name": "bad", "portfolio_ids": [], "mode": "real"})
    body = resp.json()
    assert body["code"] != 0
    assert "mode" in body["message"]


# ---- 会话级实盘资金基准（live_capital）：None=组合初始资金；设置=全组合以该值起账 ----

def test_create_session_persists_live_capital(client, mock_bridge):
    """create_session 落库 live_capital；list/detail 序列化返回；未传 → None（现行为）。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid = _create_session(c, portfolio_ids=(ps_id,), live_capital=1000000)
    detail = c.get("/api/live/sessions/%d" % sid).json()["data"]
    assert float(detail["live_capital"]) == 1000000.0
    row = next(s for s in c.get("/api/live/sessions").json()["data"] if s["id"] == sid)
    assert float(row["live_capital"]) == 1000000.0

    # 未传 → None（用组合初始资金 = 现行为）
    sid2 = _create_session(c, portfolio_ids=(ps_id,))
    detail2 = c.get("/api/live/sessions/%d" % sid2).json()["data"]
    assert detail2["live_capital"] is None


def test_create_session_rejects_non_positive_live_capital(client, mock_bridge):
    """live_capital 提供（非 None）时须 >0：0 / 负数 / 非数值 → 400 业务错误。"""
    c, _ = client
    for bad in (0, -100, "abc"):
        resp = c.post("/api/live/sessions", json={
            "name": "bad-cap", "portfolio_ids": [], "live_capital": bad,
        })
        body = resp.json()
        assert body["code"] == 400
        assert "实盘资金基准须大于 0" in body["message"]


def test_build_engine_applies_live_capital_override(client, monkeypatch, caplog):
    """build_engine 把 session.live_capital 传给 assemble_portfolio：
    override 生效 → Account 以基准资金起账；未设置 → 组合初始资金（现行为）。"""
    import logging
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    real_cls = live_api.HttpBridgeDispatcher

    def fake_constructor(base_url="http://127.0.0.1:8790", **kw):
        client_ = httpx.Client(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"ok": True})))
        return real_cls(base_url=base_url, client=client_)

    monkeypatch.setattr(live_api, "HttpBridgeDispatcher", fake_constructor)

    sid = _create_session(c, portfolio_ids=(ps_id,), live_capital=500000)
    db2 = Session()
    try:
        with caplog.at_level(logging.INFO, logger="core.services.live_service"):
            engine = live_api._build_engine(sid, db2, "simulation")
        assert engine.portfolios[0].account.initial_capital == Decimal("500000")
        assert engine.portfolios[0].account.cash == Decimal("500000")
        assert any("实盘资金基准" in r.getMessage() for r in caplog.records)
    finally:
        db2.close()

    # 未设置 live_capital → 组合配置初始资金 100000（现行为不变）
    sid2 = _create_session(c, portfolio_ids=(ps_id,))
    db3 = Session()
    try:
        engine2 = live_api._build_engine(sid2, db3, "simulation")
        assert engine2.portfolios[0].account.initial_capital == Decimal("100000")
    finally:
        db3.close()


def test_start_session_rejected_when_bridge_offline(client, monkeypatch):
    """桥未启动时 /start 返回 503 业务错误，不建引擎、不置 running。

    防回归：「一点启动就成功、桥离线却无告警」——启动前必须探测桥在线。
    """
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    real_cls = live_api.HttpBridgeDispatcher

    def fake_constructor(base_url="http://127.0.0.1:8790", **kw):
        # /ping 走 MockTransport 但返回 503 → heartbeat() 见 status_code != 200 返回 False
        client_ = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(503)))
        return real_cls(base_url=base_url, client=client_)

    monkeypatch.setattr(live_api, "HttpBridgeDispatcher", fake_constructor)

    # 同上（A-6）：显式 engine=iquant，验证 iQuant 桥离线 503，不依赖全局默认引擎。
    sid = _create_session(c, portfolio_ids=(ps_id,), mode="simulation", engine="iquant")
    resp = c.post("/api/live/sessions/%d/start" % sid)
    body = resp.json()
    assert body["code"] == 503
    assert "桥未启动" in body["message"]
    # 未建引擎、session 仍非 running
    assert sid not in live_api._ENGINES
    detail = c.get("/api/live/sessions/%d" % sid).json()["data"]
    assert detail["status"] != "running"


# ---- §8.3 手动恢复熔断 ----

def _set_breaker(Session, sid, ps_id, count=3):
    """手动把 LiveSessionPortfolio 置为 circuit_broken + count。"""
    db = Session()
    try:
        link = db.query(LiveSessionPortfolio).filter_by(
            session_id=sid, portfolio_strategy_id=ps_id
        ).first()
        assert link is not None
        link.circuit_breaker_count = count
        link.status = "circuit_broken"
        db.commit()
    finally:
        db.close()


def test_recover_breaker_resets_running_session(client, mock_bridge):
    """运行中 session：POST recover → 引擎内存态 + DB 双写解除熔断。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid = _create_session(c, portfolio_ids=(ps_id,))
    c.post("/api/live/sessions/%d/start" % sid)
    engine = live_api._ENGINES[sid]

    # 手动置 DB + 引擎内存态为熔断
    _set_breaker(Session, sid, ps_id, count=3)
    port = next(p for p in engine.portfolios if p.portfolio_id == ps_id)
    rm = port.risk_manager
    rm.consecutive_drawdown_triggers = 3
    rm.manual_recovery = True
    rm.circuit_breaker_active = True
    engine._breaker_count_written[ps_id] = 3

    resp = c.post("/api/live/sessions/%d/portfolios/%d/recover" % (sid, ps_id))

    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "active"
    assert body["data"]["circuit_breaker_count"] == 0
    # 内存态
    assert rm.manual_recovery is False
    assert rm.circuit_breaker_active is False
    assert rm.consecutive_drawdown_triggers == 0
    assert rm.is_trading_halted() is False
    assert engine._breaker_count_written[ps_id] == 0
    # DB 态
    db = Session()
    try:
        link = db.query(LiveSessionPortfolio).filter_by(
            session_id=sid, portfolio_strategy_id=ps_id
        ).first()
        assert link.status == "active"
        assert link.circuit_breaker_count == 0
    finally:
        db.close()

    c.post("/api/live/sessions/%d/stop" % sid)


def test_recover_breaker_stopped_session_only_db(client, mock_bridge):
    """未运行 session：POST recover → 只改 DB（引擎未运行，无内存态可重置）。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid = _create_session(c, portfolio_ids=(ps_id,))
    assert sid not in live_api._ENGINES  # 未 start
    _set_breaker(Session, sid, ps_id, count=3)

    resp = c.post("/api/live/sessions/%d/portfolios/%d/recover" % (sid, ps_id))

    assert resp.status_code == 200
    assert resp.json()["code"] == 0
    assert resp.json()["data"]["status"] == "active"
    assert resp.json()["data"]["circuit_breaker_count"] == 0
    db = Session()
    try:
        link = db.query(LiveSessionPortfolio).filter_by(
            session_id=sid, portfolio_strategy_id=ps_id
        ).first()
        assert link.status == "active"
        assert link.circuit_breaker_count == 0
    finally:
        db.close()


def test_recover_breaker_session_not_found(client, mock_bridge):
    """session 不存在 → 404。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    resp = c.post("/api/live/sessions/9999/portfolios/%d/recover" % ps_id)

    assert resp.status_code == 200
    assert resp.json()["code"] == 404


def test_recover_breaker_portfolio_not_in_session(client, mock_bridge):
    """组合不在该 session → 404。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid = _create_session(c, portfolio_ids=(ps_id,))

    resp = c.post("/api/live/sessions/%d/portfolios/8888/recover" % sid)

    assert resp.status_code == 200
    assert resp.json()["code"] == 404


# ---------------- C1（终审）：start check-then-act 竞态闭合 ----------------

def _conc_session_factory(tmp_path):
    """并发 start 用文件 SQLite 工厂：两个并发请求各持独立连接（fixture 的 StaticPool
    单连接不允许跨 session 并发事务），WAL 降低读写锁冲突。"""
    from sqlalchemy import event as sa_event

    eng = create_engine(
        "sqlite:///%s" % (tmp_path / "conc-start.db"),
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @sa_event.listens_for(eng, "connect")
    def _pragma(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    Base.metadata.create_all(eng)
    return sessionmaker(bind=eng, expire_on_commit=False)


def test_concurrent_start_same_session_registers_single_engine(
        client, mock_bridge, monkeypatch, tmp_path):
    """C1：并发两次 start 同一 session（threadpool 启动窗口内重叠）→ 仅一个引擎注册，
    svc.start_session 只被调用一次，第二次得幂等 running（不产生双引擎并发下单）。"""
    import asyncio
    import threading

    import core.services.live_service as live_svc

    ConcSession = _conc_session_factory(tmp_path)
    # 独立连接工厂供引擎组装/recover 使用（替代 fixture 的 StaticPool 单连接）
    monkeypatch.setattr(live_api, "SessionLocal", ConcSession)

    from core.models import LiveSession as _LiveSession

    db = ConcSession()
    ps_id = _seed(db)
    session = _LiveSession(
        name="conc-start", mode="simulation", status="stopped",
        engine="iquant", execution_mode="auto",
    )
    db.add(session)
    db.flush()
    db.add(LiveSessionPortfolio(session_id=session.id, portfolio_strategy_id=ps_id))
    db.commit()
    sid = session.id
    db.close()

    real_start = live_svc.start_session
    entered = threading.Event()
    release = threading.Event()
    calls = {"n": 0}

    def slow_start(*args, **kwargs):
        # 复刻竞态窗口：svc.start_session 在 threadpool 里秒级阻塞（heartbeat/recover）
        calls["n"] += 1
        entered.set()
        release.wait(timeout=10)
        return real_start(*args, **kwargs)

    monkeypatch.setattr(live_svc, "start_session", slow_start)

    async def call_start(session_factory):
        db_req = session_factory()
        try:
            return await live_api.start_session(sid, db_req)
        finally:
            db_req.close()

    async def drive():
        t1 = asyncio.create_task(call_start(ConcSession))
        while not entered.is_set():
            await asyncio.sleep(0.01)
        # 第一个 start 仍在 threadpool 窗口内（未注册）→ 并发发起第二个
        t2 = asyncio.create_task(call_start(ConcSession))
        await asyncio.sleep(0.2)
        # C1 断言：锁生效——第二个 start 在锁外等待，未进入启动临界区
        assert calls["n"] == 1, (
            "并发第二个 start 进入了启动临界区（_START_LOCK 未生效）"
        )
        release.set()
        r1, r2 = await asyncio.gather(t1, t2)
        engine = live_api._ENGINES.get(sid)
        await asyncio.sleep(0.05)  # 让 create_task(engine.start()) 推进到可停状态
        if engine is not None:
            await engine.stop()
        return r1, r2

    r1, r2 = asyncio.run(drive())

    assert r1["code"] == 0 and r1["data"]["status"] == "running"
    assert r2["code"] == 0 and r2["data"]["status"] == "running"  # 幂等语义
    assert calls["n"] == 1  # 第二次 start 走幂等路径，svc.start_session 仅调用一次
    assert list(live_api._ENGINES) == [sid]  # 仅注册一个引擎（无双引擎并发下单）

