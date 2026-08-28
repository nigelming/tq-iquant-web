"""实盘 service 层 — 业务逻辑（从 core.api.live 提取）。

与 core.services.backtest_service 同一范式：路由层仅留 HTTP 壳（参数校验 +
守卫/调度 + 调 service + ok/err 包装），业务逻辑下沉到本模块。本模块承接：

- 引擎组装：build_engine（查 link/PS/strategy/股票池/Formula，算 code_period_count，
  建 dispatcher/poller/LiveEngine）
- 进程注册表：ENGINES（session_id -> LiveEngine）+ B6 全局限 1 个 session 守卫
- session CRUD：list/create/get/delete + start/stop 业务体
- 熔断手动恢复：recover_breaker（运行中双写 / 未运行只改 DB）
- 历史查询 + 序列化：orders/trades/positions（运行中读内存，停止后从 live_trades 重放）

SSE 流（_sse_line/_heartbeat_stream/session_stream）是 HTTP 传输层关注点，留路由。
asyncio 任务调度（create_task(engine.start/stop)）同样留 async 路由——本模块的
start_session 只返回构造好、heartbeat 通过、recover 已执行但尚未 start() 的引擎，
由路由负责调度与注册。

纯重构，行为不变。
"""

import logging
from datetime import datetime
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from core.config import load_config
from core.db import SessionLocal
from core.models import (
    LiveSession, LiveSessionPortfolio, PortfolioStrategy, Strategy,
    StockPoolStock, Formula, LiveOrder, LiveTrade, LiveDecisionEvent,
)
from core.engine.portfolio_builder import assemble_portfolio
from core.engine.http_bridge_dispatcher import HttpBridgeDispatcher
from core.engine.bar_poller import BarPoller
from core.engine.live_engine import LiveEngine
from core.engine.decision import summarize_decisions
from core.engine.event import TradeEvent
from core.engine.position import Position
from core.tq.formula import TQFormula
from tq_iquant_shared.constants import TradeType

logger = logging.getLogger(__name__)

# 进程内运行中的 LiveEngine 注册表：session_id -> LiveEngine。
# 实盘引擎需长期持有 dispatcher/poller/portfolio 内存态，不能按请求建销，
# 故放进程级 dict（单用户系统，无需分布式协调）。
ENGINES: Dict[int, LiveEngine] = {}


# ---------------------------------------------------------------------------
# 桥配置 / 股票池 / 引擎组装
# ---------------------------------------------------------------------------

def bridge_config() -> dict:
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


def resolve_stock_codes(db: Session, portfolio_strategy_id: int) -> list:
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


def build_engine(
    session_id: int,
    db: Session,
    mode: str = "simulation",
    db_session_factory=SessionLocal,
    dispatcher_cls=None,
) -> LiveEngine:
    """组装一个 session 的 LiveEngine：各组合 Portfolio + 桥 dispatcher + BarPoller。

    公式注入（0010）：遍历各组合策略，按 Strategy.formula_id → Formula.name 预加载
    {strategy_id: formula_name}，连同 TQFormula 实例传入 LiveEngine，供 _fill_signal_cache
    逐 bar 内存注入算公式信号。无公式的策略不入映射（_fill_signal_cache 跳过）。

    dispatcher_cls：注入桥 dispatcher 类（测试 monkeypatch 用）；None 用真实类。
    """
    if dispatcher_cls is None:
        dispatcher_cls = HttpBridgeDispatcher
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
        pool_codes = resolve_stock_codes(db, ps.id)
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

    br = bridge_config()
    # 按 session.mode 选桥：simulation→仿真桥(8790/虚拟资金)，live→实盘桥(8791/真实资金)。
    # 未知 mode 兜底仿真桥（防误传走到真实资金桥）。
    base_url = br.get(mode, br["simulation"])
    dispatcher = dispatcher_cls(base_url=base_url)
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
        db_session_factory=db_session_factory,
        tq_formula=TQFormula(),
        formula_by_strategy=formula_by_strategy,
        formula_count=200,
        formula_count_by_name=formula_count_by_name,
        code_period_count=code_period_count,
    )


# ---------------------------------------------------------------------------
# session CRUD
# ---------------------------------------------------------------------------

def list_sessions(db: Session) -> list:
    sessions = db.query(LiveSession).all()
    # 一次取全部分组关系,避免 N+1(单用户规模可忽略,但分组写更干净)
    links = db.query(LiveSessionPortfolio).all()
    by_session: Dict[int, list] = {}
    for l in links:
        by_session.setdefault(l.session_id, []).append(
            {"portfolio_id": l.portfolio_strategy_id, "status": l.status}
        )
    return [
        {
            "id": s.id,
            "name": s.name,
            "mode": s.mode,
            "status": s.status,
            "started_at": s.started_at,
            "stopped_at": s.stopped_at,
            "portfolio_ids": sorted(p["portfolio_id"] for p in by_session.get(s.id, [])),
            "portfolios": by_session.get(s.id, []),
        }
        for s in sessions
    ]


def create_session(db: Session, data: dict, mode: str) -> dict:
    """创建 session。mode 已由路由校验为 simulation|live。"""
    session = LiveSession(name=data["name"], mode=mode, status="stopped")
    db.add(session)
    db.flush()
    for pid in data.get("portfolio_ids", []):
        link = LiveSessionPortfolio(session_id=session.id, portfolio_strategy_id=pid)
        db.add(link)
    db.commit()
    return {"id": session.id, "status": session.status}


def get_session(db: Session, session_id: int, engine: Optional[LiveEngine] = None) -> Optional[dict]:
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
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
    return data


def delete_session(db: Session, session_id: int, engines: Dict[int, LiveEngine]) -> Tuple[bool, Optional[str]]:
    """删除 session。返回 (ok, error_message)。运行中拒绝。"""
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return False, "资源不存在"
    if session.status == "running" or session_id in engines:
        return False, "session 运行中，请先停止再删除"
    db.query(LiveSessionPortfolio).filter(
        LiveSessionPortfolio.session_id == session_id
    ).delete()
    db.delete(session)
    db.commit()
    return True, None


# ---------------------------------------------------------------------------
# start / stop 业务体
# ---------------------------------------------------------------------------

def start_session(
    db: Session,
    session_id: int,
    engines: Dict[int, LiveEngine],
    mode: str,
    db_session_factory=SessionLocal,
    dispatcher_cls=None,
) -> Tuple[Optional[LiveEngine], Optional[LiveSession], Optional[Tuple[int, str]]]:
    """启动 session 的业务体。

    返回 (engine, session, error)：
    - error 为 None 且 engine 非 None 表示成功：engine 已构造、heartbeat 通过、
      recover 已执行但尚未 start()，**未改 session 状态/未 commit**。由路由负责
      asyncio.create_task(engine.start())、engines[session_id]=engine、
      置 session.status=running + commit（注册先于 commit，闭合 B6 窗口）。
    - error 为 None 且 engine 为 None：幂等命中（session 已在跑），路由直接返回 ok running。
    - 否则 error = (http_status, message)。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None, None, (404, "资源不存在")
    if session_id in engines:
        # 幂等：已在跑，直接返回（无 engine 新建，路由据此返回 ok running）
        return None, session, None
    if engines:
        # B6：全局限 1 个实盘 session（防多引擎争抢桥单线程 + 持仓归属混乱，Q1 未解）。
        running_id = next(iter(engines))
        return None, session, (409, "已有实盘会话 %d 运行中，全局限 1 个" % running_id)

    engine = build_engine(
        session_id, db, mode,
        db_session_factory=db_session_factory, dispatcher_cls=dispatcher_cls,
    )
    # 启动前探测桥在线：桥未起（iQuant 客户端未运行/策略未加载）直接拒绝，
    # 不建引擎、不置 running，避免「一点就成功、桥离线却无告警」。
    # heartbeat() 同步 httpx 调 /ping，本地 loopback 连接被拒瞬时失败，不阻塞事件循环。
    if not engine.dispatcher.heartbeat():
        mode_label = "实盘" if mode == "live" else "仿真"
        br = bridge_config()
        base_url = br.get(mode, br["simulation"])
        return None, session, (
            503,
            "桥未启动：%s（%s）— 请先在 iQuant 客户端加载并运行对应桥策略"
            % (mode_label, base_url),
        )
    # 重启恢复：从 live_trades 重放虚拟持仓/虚拟现金
    engine.recover(db)
    return engine, session, None


def stop_session(
    db: Session,
    session_id: int,
    engines: Dict[int, LiveEngine],
) -> Optional[LiveEngine]:
    """停止 session 业务体：从注册表弹出引擎、改状态、commit。

    返回弹出的 engine（供路由 asyncio.create_task(engine.stop())）；session 不存在返回 None。
    **引擎的异步 stop() 由路由调度**（本函数同步，不碰事件循环）。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    engine = engines.pop(session_id, None)
    session.status = "stopped"
    session.stopped_at = datetime.now()
    db.commit()
    return engine


# ---------------------------------------------------------------------------
# 熔断手动恢复 / 桥状态
# ---------------------------------------------------------------------------

def recover_breaker(
    db: Session,
    session_id: int,
    portfolio_id: int,
    engines: Dict[int, LiveEngine],
) -> Tuple[bool, Optional[str]]:
    """手动恢复某组合熔断（3 次转手动恢复后的人工恢复入口）。

    引擎运行中：engine.recover_breaker 负责内存态 + DB 双写；
    引擎未运行：只改 DB（下次 start → recover 读回 count=0 不补挂，组合正常）。
    返回 (ok, error_message)。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return False, "资源不存在"
    link = db.query(LiveSessionPortfolio).filter_by(
        session_id=session_id, portfolio_strategy_id=portfolio_id,
    ).first()
    if link is None:
        return False, "该组合不在此 session 中"
    engine = engines.get(session_id)
    if engine is not None:
        # 运行中：引擎方法负责内存 + DB 双写
        if not engine.recover_breaker(portfolio_id):
            return False, "该组合不在此 session 引擎中"
    else:
        # 未运行：只改 DB（下次 start → recover 只在 count>=3 转手动；count=0 不补挂）
        link.circuit_breaker_count = 0
        link.status = "active"
        db.commit()
    return True, None


def bridge_status(session_id: int, status: str, engine: Optional[LiveEngine]) -> dict:
    """桥在线状态（§11 切片5 并入）。引擎运行中取 dispatcher.heartbeat()；未运行返回 not_running。"""
    if engine is None:
        return {"online": None, "status": "not_running"}
    return {"online": engine.dispatcher.heartbeat(), "status": status}


# ---------------------------------------------------------------------------
# 历史查询 + 序列化
# ---------------------------------------------------------------------------

def serialize_order(o: LiveOrder) -> dict:
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


def serialize_trade(t: LiveTrade) -> dict:
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


def aggregate_positions_from_trades(trades: List[LiveTrade]) -> List[dict]:
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


def engine_virtual_positions(engine: LiveEngine) -> List[dict]:
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


def session_orders(db: Session, session_id: int, status: Optional[str] = None) -> Optional[List[dict]]:
    """委托历史。session 不存在返回 None。可选 status 过滤。"""
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    q = db.query(LiveOrder).filter(LiveOrder.live_session_id == session_id)
    if status:
        q = q.filter(LiveOrder.status == status)
    rows = q.order_by(LiveOrder.created_at.desc(), LiveOrder.id.desc()).all()
    return [serialize_order(o) for o in rows]


def session_trades(db: Session, session_id: int) -> Optional[List[dict]]:
    """成交历史，按 trade_time 倒序。session 不存在返回 None。"""
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    rows = (
        db.query(LiveTrade)
        .filter(LiveTrade.live_session_id == session_id)
        .order_by(LiveTrade.trade_time.desc(), LiveTrade.id.desc())
        .all()
    )
    return [serialize_trade(t) for t in rows]


def session_positions(db: Session, session_id: int, engine: Optional[LiveEngine]) -> Optional[List[dict]]:
    """虚拟持仓。运行中读引擎内存态（含未落库当日变动）；停止后从 live_trades 重放聚合。

    session 不存在返回 None。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    if engine is not None:
        return engine_virtual_positions(engine)
    trades = (
        db.query(LiveTrade)
        .filter(LiveTrade.live_session_id == session_id)
        .order_by(LiveTrade.trade_time, LiveTrade.id)
        .all()
    )
    return aggregate_positions_from_trades(trades)


def serialize_decision(d: LiveDecisionEvent) -> dict:
    """决策闸门事件序列化（实盘/回测同字段口径）。"""
    return {
        "id": d.id,
        "gate": d.gate,
        "layer": d.layer,
        "action": d.action,
        "portfolio_id": d.portfolio_id,
        "strategy_id": d.strategy_id,
        "stock_code": d.stock_code,
        "bar_time": d.bar_time.isoformat() if d.bar_time else None,
        "param_name": d.param_name,
        "param_value": d.param_value,
        "actual_value": d.actual_value,
        "requested_qty": d.requested_qty,
        "final_qty": d.final_qty,
        "message": d.message,
        "created_at": d.created_at.isoformat() if d.created_at else None,
    }


def session_decisions(db: Session, session_id: int) -> Optional[dict]:
    """决策闸门事件：聚合统计 + 原始事件。session 不存在返回 None。

    summary 复用 summarize_decisions（与回测详情同口径），按 (gate, param_name)
    分组计数；events 按 bar_time 正序返回（下钻看逐次触发）。
    """
    session = db.query(LiveSession).filter(LiveSession.id == session_id).first()
    if not session:
        return None
    rows = (
        db.query(LiveDecisionEvent)
        .filter(LiveDecisionEvent.live_session_id == session_id)
        .order_by(LiveDecisionEvent.bar_time, LiveDecisionEvent.id)
        .all()
    )
    return {
        "summary": summarize_decisions(rows),
        "events": [serialize_decision(d) for d in rows],
    }
