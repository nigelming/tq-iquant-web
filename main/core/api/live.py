import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from core.api.response import err, ok
from core.db import SessionLocal, get_db
from core.models import LiveSession
from core.engine.http_bridge_dispatcher import HttpBridgeDispatcher
from core.engine.live_engine import LiveEngine
# 业务逻辑已下沉到 core.services.live_service（与 backtest_service 同一范式）。
# 路由层仅留 HTTP 壳：参数校验 + asyncio 任务调度 + 调 service + ok/err 翻译。
from core.services import live_service as svc

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/live", tags=["live"])


# ---------------------------------------------------------------------------
# 兼容 re-export：集成测试 test_live_engine_api.py 直接引用以下 live.py 符号
# （对齐 core.api.backtest 的 re-export 范式）。
# ---------------------------------------------------------------------------
# 进程注册表 re-export 为 service 持有的同一个 dict——
# live_api._ENGINES.clear() / `sid in live_api._ENGINES` 仍作用于同一对象。
_ENGINES: Dict[int, LiveEngine] = svc.ENGINES

from core.services.live_service import (  # noqa: E402
    bridge_config as _bridge_config,
    resolve_stock_codes as _resolve_stock_codes,
    serialize_order as _serialize_order,
    serialize_trade as _serialize_trade,
    aggregate_positions_from_trades as _aggregate_positions_from_trades,
    engine_virtual_positions as _engine_virtual_positions,
)


def _build_engine(session_id: int, db: Session, mode: str = "simulation") -> LiveEngine:
    """薄 wrapper：绑定 db_session_factory=SessionLocal + 本模块的 HttpBridgeDispatcher。

    测试 monkeypatch live_api.SessionLocal / live_api.HttpBridgeDispatcher 后，
    直接调 live_api._build_engine(sid, db) 仍走测试库 + mock 桥（4 处 test_build_engine_* 直调）。
    """
    return svc.build_engine(
        session_id, db, mode,
        db_session_factory=SessionLocal, dispatcher_cls=HttpBridgeDispatcher,
    )


# ---------------------------------------------------------------------------
# 路由：session CRUD
# ---------------------------------------------------------------------------

@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db)):
    return ok(svc.list_sessions(db))


@router.post("/sessions")
def create_session(data: dict, db: Session = Depends(get_db)):
    mode = data.get("mode", "simulation")
    if mode not in ("simulation", "live"):
        return err(400, "mode 非法，仅支持 simulation(仿真) / live(实盘)")
    return ok(svc.create_session(db, data, mode))


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)):
    engine = _ENGINES.get(session_id)
    data = svc.get_session(db, session_id, engine)
    if data is None:
        return err(404, "资源不存在")
    return ok(data)


@router.post("/sessions/{session_id}/start")
async def start_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return err(404, "资源不存在")
    # 幂等：已在跑直接返回（service 检测 session_id in _ENGINES）
    if session_id in _ENGINES:
        return ok({"id": session.id, "status": "running"})

    engine, _session, error = svc.start_session(
        db, session_id, _ENGINES, session.mode,
        db_session_factory=SessionLocal, dispatcher_cls=HttpBridgeDispatcher,
    )
    if error is not None:
        status, message = error
        return err(status, message)
    if engine is None:
        # 幂等路径（理论上上方已拦，兜底）
        return ok({"id": session.id, "status": "running"})

    # 起 asyncio 循环任务（async 端点内直接在 FastAPI 事件循环建任务）
    asyncio.create_task(engine.start())
    # 注册先于 commit，闭合 B6 窗口（与重构前顺序一致）
    _ENGINES[session_id] = engine
    session.status = "running"
    session.started_at = datetime.now()
    db.commit()
    return ok({"id": session.id, "status": "running"})


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int, db: Session = Depends(get_db)):
    engine = svc.stop_session(db, session_id, _ENGINES)
    if engine is None:
        return err(404, "资源不存在")
    asyncio.create_task(engine.stop())
    return ok({"id": session_id, "status": "stopped"})


@router.post("/sessions/{session_id}/portfolios/{portfolio_id}/recover")
def recover_breaker(session_id: int, portfolio_id: int, db: Session = Depends(get_db)):
    ok_flag, message = svc.recover_breaker(db, session_id, portfolio_id, _ENGINES)
    if not ok_flag:
        return err(404, message)
    return ok({"portfolio_id": portfolio_id, "status": "active", "circuit_breaker_count": 0})


@router.get("/sessions/{session_id}/bridge-status")
def bridge_status(session_id: int, db: Session = Depends(get_db)):
    """桥在线状态（§11 切片5 并入，本切片先放最小端点）。

    引擎运行中取 dispatcher.heartbeat()；未运行返回 unknown。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return err(404, "资源不存在")
    engine = _ENGINES.get(session_id)
    return ok(svc.bridge_status(session_id, session.status, engine))


# ---- B4b: 历史查询端点（orders / trades / positions）----

@router.get("/sessions/{session_id}/orders")
def session_orders(session_id: int, status: Optional[str] = None, db: Session = Depends(get_db)):
    """B4b：委托历史。可选 ?status= 过滤（submitted/filled/partial/rejected/canceled）。"""
    rows = svc.session_orders(db, session_id, status)
    if rows is None:
        return err(404, "资源不存在")
    return ok(rows)


@router.get("/sessions/{session_id}/trades")
def session_trades(session_id: int, db: Session = Depends(get_db)):
    """B4b：成交历史，按 trade_time 倒序。"""
    rows = svc.session_trades(db, session_id)
    if rows is None:
        return err(404, "资源不存在")
    return ok(rows)


@router.get("/sessions/{session_id}/positions")
def session_positions(session_id: int, db: Session = Depends(get_db)):
    """B4b：虚拟持仓。运行中读引擎内存态（含未落库当日变动）；停止后从 live_trades
    重放聚合（与 recover 同口径：BUY 加、SELL 减，均价加权）。"""
    rows = svc.session_positions(db, session_id, _ENGINES.get(session_id))
    if rows is None:
        return err(404, "资源不存在")
    return ok(rows)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int, db: Session = Depends(get_db)):
    ok_flag, message = svc.delete_session(db, session_id, _ENGINES)
    if not ok_flag:
        status = 409 if message and "运行中" in message else 404
        return err(status, message)
    return ok()


# ---------------------------------------------------------------------------
# SSE 事件流（HTTP 传输层，留路由）
# ---------------------------------------------------------------------------

def _sse_line(ev: dict) -> str:
    """把引擎事件 dict 格式化为一条 SSE：event: <type>\ndata: <json>\n\n。

    data 去掉 type 字段（type 进 event 行，design §5.6.10）；ensure_ascii=False
    让中文信号名（如 signal_name）原样下发。
    """
    ev_type = ev.get("type", "message")
    data = {k: v for k, v in ev.items() if k != "type"}
    return "event: %s\ndata: %s\n\n" % (ev_type, json.dumps(data, ensure_ascii=False))


async def _heartbeat_stream(request: Request):
    """未运行 session 的 /stream：仅 30s ping 保活心跳（EventSource 自动重连，无需重连逻辑）。"""
    while True:
        if await request.is_disconnected():
            break
        yield _sse_line({"type": "ping", "time": datetime.now().isoformat()})
        await asyncio.sleep(30)


@router.get("/sessions/{session_id}/stream")
async def session_stream(session_id: int, request: Request, db: Session = Depends(get_db)):
    """实盘 session 实时事件流（SSE，design §5.6.10）。

    运行中：转发引擎 stream_events（signal/order/trade/position/risk 五类事件，
    空闲 30s 由引擎内置 ping 心跳）；未运行：仅 ping 保活。
    每个连接绑定一个 session（无鉴权，EventSource 断线自动重连）。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        # 用 HTTPException(404) 而非 body-code:EventSource 需真实 HTTP 错误码才触发 onerror,
        # body-code + HTTP 200 会让 EventSource 静默失败无提示。
        raise HTTPException(status_code=404, detail="session 不存在")

    engine = _ENGINES.get(session_id)

    async def event_generator():
        if engine is not None:
            agen = engine.stream_events()
            try:
                async for ev in agen:
                    if await request.is_disconnected():
                        break
                    yield _sse_line(ev)
            finally:
                await agen.aclose()
        else:
            async for line in _heartbeat_stream(request):
                yield line

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
