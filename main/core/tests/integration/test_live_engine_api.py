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
from sqlalchemy.pool import StaticPool

from core.main import app
from core.db import get_db
from core.models import (
    Base, StockPool, StockPoolStock, Formula, FormulaSignal,
    PortfolioStrategy, Strategy, LiveOrder, LiveTrade, LiveSessionPortfolio,
)
from core.engine.position import Position
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

    def fake_constructor(base_url="http://127.0.0.1:8790", **kw):
        client = httpx.Client(transport=httpx.MockTransport(rec.handler))
        return real_cls(base_url=base_url, client=client)

    monkeypatch.setattr(live_api, "HttpBridgeDispatcher", fake_constructor)
    return rec


def _create_session(c, name="live-test", portfolio_ids=(1,), mode="simulation"):
    resp = c.post("/api/live/sessions", json={"name": name, "portfolio_ids": list(portfolio_ids), "mode": mode})
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


def test_start_second_session_rejected_while_one_running(client, mock_bridge):
    """B6：全局限 1 个实盘 session——已有 session 运行，新 session start 被拒。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()

    sid1 = _create_session(c, portfolio_ids=(ps_id,))
    sid2 = _create_session(c, portfolio_ids=(ps_id,))

    assert c.post("/api/live/sessions/%d/start" % sid1).json()["data"]["status"] == "running"
    assert sid1 in live_api._ENGINES

    resp = c.post("/api/live/sessions/%d/start" % sid2)
    assert resp.status_code == 200  # 统一响应格式，业务错误在 body.code
    assert resp.json()["code"] != 0
    assert sid2 not in live_api._ENGINES

    c.post("/api/live/sessions/%d/stop" % sid1)


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
        gen = live_api._heartbeat_stream(_FakeRequest())
        it = gen.__aiter__()
        first = await it.__anext__()
        assert first.startswith("event: ping\ndata: ")
        assert "\"time\"" in first
        await gen.aclose()

    asyncio.run(drive())


def test_stream_endpoint_pings_when_not_running(client, mock_bridge, monkeypatch):
    """B5：未运行 session → /stream 走 _heartbeat_stream，返回 SSE 心跳。"""
    c, Session = client
    db = Session()
    ps_id = _seed(db)
    db.close()
    sid = _create_session(c, portfolio_ids=(ps_id,))

    async def fake_heartbeat(request):
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
    sid = _create_session(c)
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
    sid = _create_session(c)
    resp = c.get("/api/live/sessions/%d/orders" % sid)
    assert resp.json()["code"] == 0
    assert resp.json()["data"] == []


def test_query_trades_history(client, mock_bridge):
    """GET /sessions/{id}/trades → 成交明细按 trade_time 倒序。"""
    c, Session = client
    sid = _create_session(c)
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
    sid = _create_session(c)
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
    sid = _create_session(c)
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

    # simulation / live：创建对应 mode 的 session 后直接调 _build_engine
    for mode, expected in [("simulation", "http://127.0.0.1:8790"),
                           ("live", "http://127.0.0.1:8791")]:
        sid = _create_session(c, portfolio_ids=(ps_id,), mode=mode)
        db2 = Session()
        try:
            live_api._build_engine(sid, db2, mode)
        finally:
            db2.close()
        assert captured["base_url"] == expected, "mode=%s 应选 %s，实际 %s" % (mode, expected, captured["base_url"])

    # unknown：create_session 白名单会拒，用已存在的 simulation session 直接调 _build_engine
    # 传 unknown mode，验证兜底走仿真桥（防误传走到真实资金桥）。
    sim_sid = _create_session(c, portfolio_ids=(ps_id,), mode="simulation")
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

    sid = _create_session(c, portfolio_ids=(ps_id,), mode="live")
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

    sid = _create_session(c, portfolio_ids=(ps_id,), mode="simulation")
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

