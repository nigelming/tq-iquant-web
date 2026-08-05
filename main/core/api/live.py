import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from core.config import load_config
from core.db import SessionLocal, get_db
from core.models import (
    LiveSession, LiveSessionPortfolio, PortfolioStrategy, Strategy,
    StockPool, StockPoolStock, Formula,
)
from core.engine.portfolio_builder import assemble_portfolio
from core.engine.http_bridge_dispatcher import HttpBridgeDispatcher
from core.engine.bar_poller import BarPoller
from core.engine.live_engine import LiveEngine
from core.tq.formula import TQFormula

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/live", tags=["live"])

# 进程内运行中的 LiveEngine 注册表：session_id -> LiveEngine。
# 实盘引擎需长期持有 dispatcher/poller/portfolio 内存态，不能按请求建销，
# 故放进程级 dict（单用户系统，无需分布式协调）。
_ENGINES: Dict[int, LiveEngine] = {}


def _bridge_config() -> dict:
    """读桥地址/token：config.iquant_bridge 段，缺省 127.0.0.1:8790。"""
    cfg = load_config()
    br = cfg.get("iquant_bridge", {}) if isinstance(cfg, dict) else {}
    return {
        "base_url": br.get("base_url", "http://127.0.0.1:8790"),
        "token": br.get("token"),
    }


def _resolve_stock_codes(db: Session, portfolio_strategy_id: int) -> list:
    """取某组合股票池的成分股代码列表（BarPoller 行情订阅范围）。"""
    ps = db.query(PortfolioStrategy).filter_by(id=portfolio_strategy_id).first()
    if ps is None:
        return []
    stocks = (
        db.query(StockPoolStock)
        .filter_by(pool_id=ps.stock_pool_id)
        .all()
    )
    return [s.stock_code for s in stocks if s.stock_code]


@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db)):
    sessions = db.query(LiveSession).all()
    return {
        "code": 0,
        "data": [
            {
                "id": s.id,
                "name": s.name,
                "mode": s.mode,
                "status": s.status,
                "started_at": s.started_at,
                "stopped_at": s.stopped_at,
            }
            for s in sessions
        ],
    }


@router.post("/sessions")
def create_session(data: dict, db: Session = Depends(get_db)):
    session = LiveSession(
        name=data["name"],
        mode=data.get("mode", "simulation"),
        status="stopped",
    )
    db.add(session)
    db.flush()
    for pid in data.get("portfolio_ids", []):
        link = LiveSessionPortfolio(session_id=session.id, portfolio_strategy_id=pid)
        db.add(link)
    db.commit()
    return {"code": 0, "data": {"id": session.id, "status": session.status}}


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return {"code": 404, "message": "资源不存在"}
    portfolios = (
        db.query(LiveSessionPortfolio)
        .filter(LiveSessionPortfolio.session_id == session_id)
        .all()
    )
    return {
        "code": 0,
        "data": {
            "id": session.id,
            "name": session.name,
            "mode": session.mode,
            "status": session.status,
            "portfolios": [
                {"portfolio_id": p.portfolio_strategy_id, "status": p.status}
                for p in portfolios
            ],
        },
    }


def _build_engine(session_id: int, db: Session) -> LiveEngine:
    """组装一个 session 的 LiveEngine：各组合 Portfolio + 桥 dispatcher + BarPoller。

    公式注入（0010）：遍历各组合策略，按 Strategy.formula_id → Formula.name 预加载
    {strategy_id: formula_name}，连同 TQFormula 实例传入 LiveEngine，供 _fill_signal_cache
    逐 bar 内存注入算公式信号。无公式的策略不入映射（_fill_signal_cache 跳过）。
    """
    links = (
        db.query(LiveSessionPortfolio)
        .filter(LiveSessionPortfolio.session_id == session_id)
        .all()
    )
    portfolios = []
    stock_codes: set = set()
    formula_ids: set = set()
    strategy_formula: Dict[int, int] = {}  # strategy_id -> formula_id
    for link in links:
        ps = db.query(PortfolioStrategy).filter_by(id=link.portfolio_strategy_id).first()
        if ps is None:
            continue
        strategies = db.query(Strategy).filter_by(portfolio_id=ps.id).all()
        port = assemble_portfolio(ps, strategies, db)
        portfolios.append(port)
        stock_codes.update(_resolve_stock_codes(db, ps.id))
        for strat in strategies:
            if strat.formula_id is not None:
                formula_ids.add(strat.formula_id)
                strategy_formula[strat.id] = strat.formula_id

    # 批量查 Formula，建 {strategy_id: formula_name}
    formula_by_strategy: Dict[int, str] = {}
    if formula_ids:
        formula_map = {
            f.id: f.name
            for f in db.query(Formula).filter(Formula.id.in_(formula_ids)).all()
        }
        for sid, fid in strategy_formula.items():
            name = formula_map.get(fid)
            if name:
                formula_by_strategy[sid] = name

    br = _bridge_config()
    dispatcher = HttpBridgeDispatcher(base_url=br["base_url"], token=br["token"])
    # 股票池为空时给一个占位，避免空列表；首期实盘一般有成分股
    poller = BarPoller(
        dispatcher, sorted(stock_codes) or ["000001.SZ"],
        period="1m", count=10,
    )
    return LiveEngine(
        session_id=session_id,
        portfolios=portfolios,
        dispatcher=dispatcher,
        bar_poller=poller,
        db_session_factory=SessionLocal,
        tq_formula=TQFormula(),
        formula_by_strategy=formula_by_strategy,
        formula_count=200,
    )


@router.post("/sessions/{session_id}/start")
async def start_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return {"code": 404, "message": "资源不存在"}
    if session_id in _ENGINES:
        return {"code": 0, "data": {"id": session.id, "status": "running"}}

    engine = _build_engine(session_id, db)
    # 重启恢复：从 live_trades 重放虚拟持仓/虚拟现金
    engine.recover(db)
    # 起 asyncio 循环任务（async 端点内直接在 FastAPI 事件循环建任务）
    asyncio.create_task(engine.start())
    _ENGINES[session_id] = engine

    session.status = "running"
    session.started_at = datetime.now()
    db.commit()
    return {"code": 0, "data": {"id": session.id, "status": "running"}}


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return {"code": 404, "message": "资源不存在"}
    engine = _ENGINES.pop(session_id, None)
    if engine is not None:
        asyncio.create_task(engine.stop())

    session.status = "stopped"
    session.stopped_at = datetime.now()
    db.commit()
    return {"code": 0, "data": {"id": session.id, "status": "stopped"}}


@router.get("/sessions/{session_id}/bridge-status")
def bridge_status(session_id: int, db: Session = Depends(get_db)):
    """桥在线状态（§11 切片5 并入，本切片先放最小端点）。

    引擎运行中取 dispatcher.heartbeat()；未运行返回 unknown。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return {"code": 404, "message": "资源不存在"}
    engine = _ENGINES.get(session_id)
    if engine is None:
        return {"code": 0, "data": {"online": None, "status": "not_running"}}
    return {"code": 0, "data": {"online": engine.dispatcher.heartbeat(), "status": session.status}}


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return {"code": 404, "message": "资源不存在"}
    db.query(LiveSessionPortfolio).filter(
        LiveSessionPortfolio.session_id == session_id
    ).delete()
    db.delete(session)
    db.commit()
    return {"code": 0}


@router.get("/sessions/{session_id}/stream")
async def session_stream(session_id: int, request: Request, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return {"code": 404, "message": "资源不存在"}

    async def event_generator():
        while True:
            if await request.is_disconnected():
                break
            yield f"event: ping\ndata: {json.dumps({'time': datetime.now().isoformat()})}\n\n"
            await asyncio.sleep(30)

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
