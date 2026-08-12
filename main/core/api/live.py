import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from core.api.response import err, ok
from core.config import load_config
from core.db import SessionLocal, get_db
from core.models import (
    LiveSession, LiveSessionPortfolio, PortfolioStrategy, Strategy,
    StockPool, StockPoolStock, Formula, LiveOrder, LiveTrade,
)
from core.engine.portfolio_builder import assemble_portfolio
from core.engine.http_bridge_dispatcher import HttpBridgeDispatcher
from core.engine.bar_poller import BarPoller
from core.engine.live_engine import LiveEngine
from core.engine.event import TradeEvent
from core.engine.position import Position
from core.tq.formula import TQFormula
from tq_iquant_shared.constants import TradeType

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/live", tags=["live"])

# 进程内运行中的 LiveEngine 注册表：session_id -> LiveEngine。
# 实盘引擎需长期持有 dispatcher/poller/portfolio 内存态，不能按请求建销，
# 故放进程级 dict（单用户系统，无需分布式协调）。
_ENGINES: Dict[int, LiveEngine] = {}


def _bridge_config() -> dict:
    """读双桥地址：config.iquant_bridge 段，simulation(仿真/虚拟资金)/live(实盘/真实资金)
    各一个 base_url。桥绑 loopback，单用户本机部署，不鉴权（token 已移除）。

    session.mode = simulation|live 决定走哪个桥（账号差异，见 live/bridge/README.md）。
    模拟/实盘（信号 vs 真实下单）不由 Core 控制，由 iQuant 客户端启动按钮决定。
    """
    cfg = load_config()
    br = cfg.get("iquant_bridge", {}) if isinstance(cfg, dict) else {}
    sim = br.get("simulation", {}) if isinstance(br, dict) else {}
    liv = br.get("live", {}) if isinstance(br, dict) else {}
    return {
        "simulation": sim.get("base_url", "http://127.0.0.1:8790"),
        "live": liv.get("base_url", "http://127.0.0.1:8791"),
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
    # 一次取全部分组关系,避免 N+1(单用户规模可忽略,但分组写更干净)
    links = db.query(LiveSessionPortfolio).all()
    by_session: Dict[int, list] = {}
    for l in links:
        by_session.setdefault(l.session_id, []).append(l.portfolio_strategy_id)
    return ok([
            {
                "id": s.id,
                "name": s.name,
                "mode": s.mode,
                "status": s.status,
                "started_at": s.started_at,
                "stopped_at": s.stopped_at,
                "portfolio_ids": sorted(by_session.get(s.id, [])),
            }
            for s in sessions
        ])


@router.post("/sessions")
def create_session(data: dict, db: Session = Depends(get_db)):
    mode = data.get("mode", "simulation")
    if mode not in ("simulation", "live"):
        return err(400, "mode 非法，仅支持 simulation(仿真) / live(实盘)")
    session = LiveSession(
        name=data["name"],
        mode=mode,
        status="stopped",
    )
    db.add(session)
    db.flush()
    for pid in data.get("portfolio_ids", []):
        link = LiveSessionPortfolio(session_id=session.id, portfolio_strategy_id=pid)
        db.add(link)
    db.commit()
    return ok({"id": session.id, "status": session.status})


@router.get("/sessions/{session_id}")
def get_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return err(404, "资源不存在")
    portfolios = (
        db.query(LiveSessionPortfolio)
        .filter(LiveSessionPortfolio.session_id == session_id)
        .all()
    )
    data = {
        "id": session.id,
        "name": session.name,
        "mode": session.mode,
        "status": session.status,
        "portfolios": [
            {"portfolio_id": p.portfolio_strategy_id, "status": p.status}
            for p in portfolios
        ],
    }
    # G7（0011 §5.11）：桥状态并入 session API。运行中取引擎实时态：
    # bridge_online 为实时心跳（/ping），pending_orders 在途单计数，last_backfill_time
    # 最近一次成交回报回填时点；未运行返回空值。
    engine = _ENGINES.get(session_id)
    if engine is not None:
        data["bridge_online"] = engine.dispatcher.heartbeat()
        data["pending_orders"] = engine.pending_orders_count
        data["last_backfill_time"] = (
            engine.last_backfill_time.strftime("%Y-%m-%d %H:%M:%S")
            if engine.last_backfill_time is not None else None
        )
    else:
        data["bridge_online"] = None
        data["pending_orders"] = 0
        data["last_backfill_time"] = None
    return ok(data)


def _build_engine(session_id: int, db: Session, mode: str = "simulation") -> LiveEngine:
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
    # 预热/分发按 (code, period) 取该股票该周期所有公式的 formula_count 最大值（跨组合跨策略）。
    # 无公式兜底 DEFAULT_COUNT(200)。预热只拉实际有策略的 (code,period)，按需不浪费。
    # 在此同一循环累加（已查 ps/strategies/股票池，不二次遍历）。
    DEFAULT_COUNT = 200
    code_period_count: Dict[tuple, int] = {}
    # 注：formula_map 在循环后批量查，此处先记 strategy_id -> period，待 formula_map 就绪再算 max。
    # 简化：先记 (code, period) -> [formula_id...]，formula_map 就绪后取 max(formula_count)。
    code_period_formula_ids: Dict[tuple, set] = {}
    for link in links:
        ps = db.query(PortfolioStrategy).filter_by(id=link.portfolio_strategy_id).first()
        if ps is None:
            continue
        strategies = db.query(Strategy).filter_by(portfolio_id=ps.id).all()
        port = assemble_portfolio(ps, strategies, db)
        portfolios.append(port)
        pool_codes = _resolve_stock_codes(db, ps.id)
        stock_codes.update(pool_codes)
        for strat in strategies:
            if strat.formula_id is not None:
                formula_ids.add(strat.formula_id)
                strategy_formula[strat.id] = strat.formula_id
            # 累加 (code, period) -> formula_id（跨组合跨策略，同 key 收集所有公式）
            for code in pool_codes:
                code_period_formula_ids.setdefault((code, strat.period), set()).add(strat.formula_id)

    # 批量查 Formula，建 {strategy_id: formula_name} + {formula_name: formula_count}
    # formula_count（Q4 决策4）为公式级注入根数：实盘注入 count 来自该字段，非全局 200。
    formula_by_strategy: Dict[int, str] = {}
    formula_count_by_name: Dict[str, int] = {}
    # formula_id -> formula_count（供 code_period_count 取 max）
    formula_count_by_id: Dict[int, int] = {}
    if formula_ids:
        formula_map = {
            f.id: f
            for f in db.query(Formula).filter(Formula.id.in_(formula_ids)).all()
        }
        for sid, fid in strategy_formula.items():
            f = formula_map.get(fid)
            if f:
                formula_by_strategy[sid] = f.name
                if f.formula_count:
                    formula_count_by_name[f.name] = f.formula_count
                    formula_count_by_id[fid] = f.formula_count

    # (code, period) -> 该股票该周期所有公式的 formula_count 最大值（跨组合跨策略）。
    # formula_id 为 None 的策略（无公式）兜底 DEFAULT_COUNT。预热/分发按此拉，按需不浪费。
    for (code, period), fids in code_period_formula_ids.items():
        counts = [formula_count_by_id.get(fid, DEFAULT_COUNT) for fid in fids if fid is not None]
        # 无任何带公式的策略 → 兜底；有则取 max
        code_period_count[(code, period)] = max(counts) if counts else DEFAULT_COUNT

    br = _bridge_config()
    # 按 session.mode 选桥：simulation→仿真桥(8790/虚拟资金)，live→实盘桥(8791/真实资金)。
    # 未知 mode 兜底仿真桥（防误传走到真实资金桥）。
    base_url = br.get(mode, br["simulation"])
    dispatcher = HttpBridgeDispatcher(base_url=base_url)
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
        formula_count_by_name=formula_count_by_name,
        code_period_count=code_period_count,
    )


@router.post("/sessions/{session_id}/start")
async def start_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return err(404, "资源不存在")
    if session_id in _ENGINES:
        return ok({"id": session.id, "status": "running"})
    if _ENGINES:
        # B6：全局限 1 个实盘 session（防多引擎争抢桥单线程 + 持仓归属混乱，Q1 未解）。
        # 任一 session 在跑即拒绝新 start（同 session 重复 start 已被上方幂等拦截）。
        running_id = next(iter(_ENGINES))
        return err(409, "已有实盘会话 %d 运行中，全局限 1 个" % running_id)

    engine = _build_engine(session_id, db, session.mode)
    # 启动前探测桥在线：桥未起（iQuant 客户端未运行/策略未加载）直接拒绝，
    # 不建引擎、不置 running，避免「一点就成功、桥离线却无告警」。
    # heartbeat() 同步 httpx 调 /ping，本地 loopback 连接被拒瞬时失败，不阻塞事件循环。
    if not engine.dispatcher.heartbeat():
        mode_label = "实盘" if session.mode == "live" else "仿真"
        br = _bridge_config()
        base_url = br.get(session.mode, br["simulation"])
        return err(503, "桥未启动：%s（%s）— 请先在 iQuant 客户端加载并运行对应桥策略"
                         % (mode_label, base_url))
    # 重启恢复：从 live_trades 重放虚拟持仓/虚拟现金
    engine.recover(db)
    # 起 asyncio 循环任务（async 端点内直接在 FastAPI 事件循环建任务）
    asyncio.create_task(engine.start())
    _ENGINES[session_id] = engine

    session.status = "running"
    session.started_at = datetime.now()
    db.commit()
    return ok({"id": session.id, "status": "running"})


@router.post("/sessions/{session_id}/stop")
async def stop_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return err(404, "资源不存在")
    engine = _ENGINES.pop(session_id, None)
    if engine is not None:
        asyncio.create_task(engine.stop())

    session.status = "stopped"
    session.stopped_at = datetime.now()
    db.commit()
    return ok({"id": session.id, "status": "stopped"})


@router.get("/sessions/{session_id}/bridge-status")
def bridge_status(session_id: int, db: Session = Depends(get_db)):
    """桥在线状态（§11 切片5 并入，本切片先放最小端点）。

    引擎运行中取 dispatcher.heartbeat()；未运行返回 unknown。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return err(404, "资源不存在")
    engine = _ENGINES.get(session_id)
    if engine is None:
        return ok({"online": None, "status": "not_running"})
    return ok({"online": engine.dispatcher.heartbeat(), "status": session.status})


# ---- B4b: 历史查询端点（orders / trades / positions）----

def _serialize_order(o: LiveOrder) -> dict:
    return {
        "id": o.id,
        "live_session_id": o.live_session_id,
        "portfolio_strategy_id": o.portfolio_strategy_id,
        "strategy_id": o.strategy_id,
        "stock_code": o.stock_code,
        "trade_type": o.trade_type,
        "order_type": o.order_type,
        "price": float(o.price) if o.price is not None else None,
        "quantity": o.quantity,
        "filled_quantity": o.filled_quantity,
        "filled_price": float(o.filled_price) if o.filled_price is not None else None,
        "status": o.status,
        "error_message": o.error_message,
        "signal_name": o.signal_name,
        "signal_type": o.signal_type,
        "bar_time": o.bar_time.isoformat() if o.bar_time else None,
        "order_ref": o.order_ref,
        "bridge_order_id": o.bridge_order_id,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "updated_at": o.updated_at.isoformat() if o.updated_at else None,
    }


def _serialize_trade(t: LiveTrade) -> dict:
    return {
        "id": t.id,
        "live_session_id": t.live_session_id,
        "live_order_id": t.live_order_id,
        "portfolio_strategy_id": t.portfolio_strategy_id,
        "strategy_id": t.strategy_id,
        "stock_code": t.stock_code,
        "trade_type": t.trade_type,
        "price": float(t.price) if t.price is not None else None,
        "quantity": t.quantity,
        "amount": float(t.amount) if t.amount is not None else None,
        "commission": float(t.commission) if t.commission is not None else None,
        "stamp_duty": float(t.stamp_duty) if t.stamp_duty is not None else None,
        "trade_time": t.trade_time.isoformat() if t.trade_time else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
    }


def _aggregate_positions_from_trades(trades: List[LiveTrade]) -> List[dict]:
    """从 live_trades 重放聚合虚拟持仓（与 recover/Position.apply_trade 同口径）。

    按 trade_time 顺序：BUY 加权累加持仓+均价，SELL 减仓均价不变。返回按 code 排序。
    """
    agg: Dict[str, Position] = {}
    for tr in trades:
        pos = agg.get(tr.stock_code)
        if pos is None:
            pos = Position(tr.stock_code)
            agg[tr.stock_code] = pos
        pos.apply_trade(TradeEvent(
            strategy_id=tr.strategy_id,
            portfolio_id=tr.portfolio_strategy_id,
            stock_code=tr.stock_code,
            trade_type=TradeType(tr.trade_type.upper()),
            price=Decimal(str(tr.price)),
            quantity=tr.quantity,
            amount=Decimal(str(tr.amount)),
            commission=Decimal(str(tr.commission)),
            stamp_duty=Decimal(str(tr.stamp_duty)),
            trade_time=tr.trade_time,
        ))
    return [
        {
            "stock_code": code,
            "quantity": pos.quantity,
            "avg_cost": float(pos.avg_cost),
            "market_value": float(pos.market_value),
        }
        for code, pos in sorted(agg.items())
        if pos.quantity != 0
    ]


def _engine_virtual_positions(engine: LiveEngine) -> List[dict]:
    """运行中：聚合引擎各组合策略虚拟持仓（按 code 汇总净仓，多组合加权均价）。

    只读引擎内存态，不修改 Position 对象；含当日已成交未落库的变动（更实时）。
    """
    agg: Dict[str, dict] = {}
    for port in engine.portfolios:
        for ctx in port.strategies:
            for code, pos in ctx.positions.items():
                if pos.quantity == 0:
                    continue
                row = agg.get(code)
                if row is None:
                    agg[code] = {"quantity": pos.quantity, "avg_cost": pos.avg_cost}
                else:
                    total = row["quantity"] + pos.quantity
                    row["avg_cost"] = (
                        row["avg_cost"] * row["quantity"] + pos.avg_cost * pos.quantity
                    ) / total
                    row["quantity"] = total
    return [
        {
            "stock_code": code,
            "quantity": r["quantity"],
            "avg_cost": float(r["avg_cost"]),
            "market_value": float(r["avg_cost"] * r["quantity"]),
        }
        for code, r in sorted(agg.items())
    ]


@router.get("/sessions/{session_id}/orders")
def session_orders(session_id: int, status: Optional[str] = None, db: Session = Depends(get_db)):
    """B4b：委托历史。可选 ?status= 过滤（submitted/filled/partial/rejected/canceled）。"""
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return err(404, "资源不存在")
    q = db.query(LiveOrder).filter(LiveOrder.live_session_id == session_id)
    if status:
        q = q.filter(LiveOrder.status == status)
    rows = q.order_by(LiveOrder.created_at.desc(), LiveOrder.id.desc()).all()
    return ok([_serialize_order(o) for o in rows])


@router.get("/sessions/{session_id}/trades")
def session_trades(session_id: int, db: Session = Depends(get_db)):
    """B4b：成交历史，按 trade_time 倒序。"""
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return err(404, "资源不存在")
    rows = (
        db.query(LiveTrade)
        .filter(LiveTrade.live_session_id == session_id)
        .order_by(LiveTrade.trade_time.desc(), LiveTrade.id.desc())
        .all()
    )
    return ok([_serialize_trade(t) for t in rows])


@router.get("/sessions/{session_id}/positions")
def session_positions(session_id: int, db: Session = Depends(get_db)):
    """B4b：虚拟持仓。运行中读引擎内存态（含未落库当日变动）；停止后从 live_trades
    重放聚合（与 recover 同口径：BUY 加、SELL 减，均价加权）。"""
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return err(404, "资源不存在")
    engine = _ENGINES.get(session_id)
    if engine is not None:
        positions = _engine_virtual_positions(engine)
    else:
        trades = (
            db.query(LiveTrade)
            .filter(LiveTrade.live_session_id == session_id)
            .order_by(LiveTrade.trade_time, LiveTrade.id)
            .all()
        )
        positions = _aggregate_positions_from_trades(trades)
    return ok(positions)


@router.delete("/sessions/{session_id}")
def delete_session(session_id: int, db: Session = Depends(get_db)):
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return err(404, "资源不存在")
    db.query(LiveSessionPortfolio).filter(
        LiveSessionPortfolio.session_id == session_id
    ).delete()
    db.delete(session)
    db.commit()
    return ok()


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
