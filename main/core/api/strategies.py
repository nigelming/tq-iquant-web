"""组合策略管理 CRUD — 组合（PortfolioStrategy）+ 嵌套子策略（Strategy）。

模型无 relationship，子列表用显式二次查询。主从策略自引用（master_strategy_id）
用两步 commit 处理：先 insert 全部子策略拿 id，再 UPDATE master_strategy_id。

前端约定：master_strategy_id=0 表示"指向本批第 0 个子策略"（新建时用临时索引），
非 0 正整数表示已存在的 strategy id（编辑时保留）。后端统一解析为真实 id。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.db import get_db
from core.models import PortfolioStrategy, Strategy, StockPool, Formula

router = APIRouter(prefix="/api/portfolios", tags=["portfolios"])

# 枚举校验集合（出处：docs/system-plan-draft.md §5.3.2）
# 周期取 TQ 公式支持 ∩ iQuant 桥 xtdata 本地读取白名单的交集（open-questions Q4）。
# 两端均已真机/源码验证：1m/5m/15m/30m/1h/1d。1w/1mon TQ 支持但桥走远程分支未真机验，暂不放行。
# 注意 60m 两端都不认（TQ periodstr error + xtdata 白名单是 1h 不是 60m）。
VALID_PERIODS = {"1m", "5m", "15m", "30m", "1h", "1d"}
VALID_ROLES = {"independent", "master", "slave"}  # draft:223
VALID_TRADING_SESSIONS = {"full", "am", "pm"}  # draft:499，非 morning/afternoon
VALID_STATUSES = {"active", "archived"}  # draft:500


class StrategyItem(BaseModel):
    name: str
    formula_id: int
    period: str
    role: str
    master_strategy_id: int | None = None  # 0=本批第N个；正整数=已存在 id；None=无
    capital_ratio: float = 0.6
    max_positions: int = 5
    single_open_ratio: float = 0.1
    stop_loss_ratio: float = 0.05
    take_profit_ratio: float = 0.15
    trailing_stop_ratio: float = 0.03
    add_position_threshold: float = 0.05
    max_add_count: int = 2
    add_position_ratio: float = 0.1
    reduce_position_ratio: float = 0.3


class PortfolioCreate(BaseModel):
    name: str
    stock_pool_id: int
    benchmark_index: str = "000300.SH"
    initial_capital: float = 500000
    max_drawdown: float = 0.2
    daily_loss_limit: float = 0.05
    max_holdings: int = 10
    min_commission: float = 5
    buy_commission_rate: float = 0.00025
    sell_commission_rate: float = 0.00025
    stamp_duty_rate: float = 0.0005
    slippage: float = 0
    trading_session: str = "full"
    status: str = "active"
    strategies: list[StrategyItem] = []


def _serialize_strategy(s: Strategy) -> dict:
    return {
        "id": s.id,
        "portfolio_id": s.portfolio_id,
        "name": s.name,
        "formula_id": s.formula_id,
        "period": s.period,
        "role": s.role,
        "master_strategy_id": s.master_strategy_id,
        "capital_ratio": float(s.capital_ratio) if s.capital_ratio is not None else None,
        "max_positions": s.max_positions,
        "single_open_ratio": float(s.single_open_ratio) if s.single_open_ratio is not None else None,
        "stop_loss_ratio": float(s.stop_loss_ratio) if s.stop_loss_ratio is not None else None,
        "take_profit_ratio": float(s.take_profit_ratio) if s.take_profit_ratio is not None else None,
        "trailing_stop_ratio": float(s.trailing_stop_ratio) if s.trailing_stop_ratio is not None else None,
        "add_position_threshold": float(s.add_position_threshold) if s.add_position_threshold is not None else None,
        "max_add_count": s.max_add_count,
        "add_position_ratio": float(s.add_position_ratio) if s.add_position_ratio is not None else None,
        "reduce_position_ratio": float(s.reduce_position_ratio) if s.reduce_position_ratio is not None else None,
    }


def _serialize_portfolio(db: Session, p: PortfolioStrategy) -> dict:
    """显式二次查询子策略（模型无 relationship）。"""
    subs = (
        db.query(Strategy)
        .filter(Strategy.portfolio_id == p.id)
        .order_by(Strategy.id)
        .all()
    )
    return {
        "id": p.id,
        "name": p.name,
        "stock_pool_id": p.stock_pool_id,
        "benchmark_index": p.benchmark_index,
        "initial_capital": float(p.initial_capital) if p.initial_capital is not None else None,
        "max_drawdown": float(p.max_drawdown) if p.max_drawdown is not None else None,
        "daily_loss_limit": float(p.daily_loss_limit) if p.daily_loss_limit is not None else None,
        "max_holdings": p.max_holdings,
        "min_commission": float(p.min_commission) if p.min_commission is not None else None,
        "buy_commission_rate": float(p.buy_commission_rate) if p.buy_commission_rate is not None else None,
        "sell_commission_rate": float(p.sell_commission_rate) if p.sell_commission_rate is not None else None,
        "stamp_duty_rate": float(p.stamp_duty_rate) if p.stamp_duty_rate is not None else None,
        "slippage": float(p.slippage) if p.slippage is not None else None,
        "trading_session": p.trading_session,
        "status": p.status,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "strategies": [_serialize_strategy(s) for s in subs],
    }


def _validate_portfolio(req: PortfolioCreate, db: Session) -> str | None:
    """返回错误消息（str）或 None（校验通过）。"""
    if req.trading_session not in VALID_TRADING_SESSIONS:
        return f"trading_session 必须为 {sorted(VALID_TRADING_SESSIONS)}，收到 {req.trading_session}"
    if req.status not in VALID_STATUSES:
        return f"status 必须为 {sorted(VALID_STATUSES)}，收到 {req.status}"
    if not (0 < req.max_drawdown < 1):
        return f"max_drawdown 必须在 (0,1)，收到 {req.max_drawdown}"
    if not (0 < req.daily_loss_limit < 1):
        return f"daily_loss_limit 必须在 (0,1)，收到 {req.daily_loss_limit}"
    if not db.query(StockPool).filter(StockPool.id == req.stock_pool_id).first():
        return f"股票池 id={req.stock_pool_id} 不存在"
    # 子策略校验
    for idx, s in enumerate(req.strategies):
        if s.role not in VALID_ROLES:
            return f"子策略[{idx}] role 必须为 {sorted(VALID_ROLES)}，收到 {s.role}"
        if s.period not in VALID_PERIODS:
            return f"子策略[{idx}] period 必须为 {sorted(VALID_PERIODS)}，收到 {s.period}"
        if not (0 < s.capital_ratio <= 1):
            return f"子策略[{idx}] capital_ratio 必须在 (0,1]，收到 {s.capital_ratio}"
        if not db.query(Formula).filter(Formula.id == s.formula_id).first():
            return f"子策略[{idx}] 公式 id={s.formula_id} 不存在"
        # 主从配置期校验
        if s.role == "slave":
            if s.master_strategy_id is None:
                return f"子策略[{idx}] role=slave 必须指定主策略（master_strategy_id）"
        else:  # master / independent
            if s.master_strategy_id is not None:
                return f"子策略[{idx}] role={s.role} 的 master_strategy_id 必须为空"
    # slave 的 master 指向必须是同批 master 角色（master_strategy_id=0..N 指本批索引）
    for idx, s in enumerate(req.strategies):
        if s.role == "slave" and s.master_strategy_id is not None and s.master_strategy_id >= 0:
            target_idx = s.master_strategy_id
            if target_idx >= len(req.strategies):
                return f"子策略[{idx}] 主策略索引 {target_idx} 超出范围"
            target = req.strategies[target_idx]
            if target.role != "master":
                return f"子策略[{idx}] 指向的主策略[{target_idx}] 角色为 {target.role}，必须为 master"
    return None


def _apply_portfolio_fields(p: PortfolioStrategy, req: PortfolioCreate) -> None:
    p.name = req.name
    p.stock_pool_id = req.stock_pool_id
    p.benchmark_index = req.benchmark_index
    p.initial_capital = req.initial_capital
    p.max_drawdown = req.max_drawdown
    p.daily_loss_limit = req.daily_loss_limit
    p.max_holdings = req.max_holdings
    p.min_commission = req.min_commission
    p.buy_commission_rate = req.buy_commission_rate
    p.sell_commission_rate = req.sell_commission_rate
    p.stamp_duty_rate = req.stamp_duty_rate
    p.slippage = req.slippage
    p.trading_session = req.trading_session
    p.status = req.status


def _create_strategies_two_step(db: Session, pid: int, strategies: list[StrategyItem]) -> None:
    """两步 commit：先 insert 全部子策略拿 id，再 UPDATE master_strategy_id。
    master_strategy_id: 0..N = 本批索引；正整数 = 已存在 id（保留）；None = 无。"""
    # 第一步：insert 全部，master_strategy_id 暂置 None
    inserted: list[Strategy] = []
    for s in strategies:
        strat = Strategy(
            portfolio_id=pid, name=s.name, formula_id=s.formula_id,
            period=s.period, role=s.role, master_strategy_id=None,
            capital_ratio=s.capital_ratio, max_positions=s.max_positions,
            single_open_ratio=s.single_open_ratio,
            stop_loss_ratio=s.stop_loss_ratio, take_profit_ratio=s.take_profit_ratio,
            trailing_stop_ratio=s.trailing_stop_ratio,
            add_position_threshold=s.add_position_threshold, max_add_count=s.max_add_count,
            add_position_ratio=s.add_position_ratio, reduce_position_ratio=s.reduce_position_ratio,
        )
        db.add(strat)
        inserted.append(strat)
    db.flush()  # 拿到所有 inserted[i].id
    # 第二步：UPDATE master_strategy_id
    for s, strat in zip(strategies, inserted):
        if s.role == "slave" and s.master_strategy_id is not None:
            if s.master_strategy_id >= 0:
                # 本批索引
                strat.master_strategy_id = inserted[s.master_strategy_id].id
            # 负整数（已存在 id）保留为 None 本次不支持，编辑场景由 PUT 走全量替换


@router.get("")
def list_portfolios(db: Session = Depends(get_db)):
    items = db.query(PortfolioStrategy).order_by(PortfolioStrategy.id).all()
    return {"code": 0, "data": [_serialize_portfolio(db, p) for p in items]}


@router.get("/{pid}")
def get_portfolio(pid: int, db: Session = Depends(get_db)):
    p = db.query(PortfolioStrategy).filter(PortfolioStrategy.id == pid).first()
    if not p:
        return {"code": 404, "message": "组合策略不存在"}
    return {"code": 0, "data": _serialize_portfolio(db, p)}


@router.post("")
def create_portfolio(req: PortfolioCreate, db: Session = Depends(get_db)):
    err = _validate_portfolio(req, db)
    if err:
        return {"code": 400, "message": err}
    p = PortfolioStrategy()
    _apply_portfolio_fields(p, req)
    db.add(p)
    db.flush()
    _create_strategies_two_step(db, p.id, req.strategies)
    db.commit()
    db.refresh(p)
    return {"code": 0, "data": _serialize_portfolio(db, p)}


@router.put("/{pid}")
def update_portfolio(pid: int, req: PortfolioCreate, db: Session = Depends(get_db)):
    p = db.query(PortfolioStrategy).filter(PortfolioStrategy.id == pid).first()
    if not p:
        return {"code": 404, "message": "组合策略不存在"}
    err = _validate_portfolio(req, db)
    if err:
        return {"code": 400, "message": err}
    _apply_portfolio_fields(p, req)
    # 子表全量替换（同 formulas.py 模式）：删旧建新
    db.query(Strategy).filter(Strategy.portfolio_id == pid).delete()
    _create_strategies_two_step(db, pid, req.strategies)
    db.commit()
    db.refresh(p)
    return {"code": 0, "data": _serialize_portfolio(db, p)}


@router.delete("/{pid}")
def delete_portfolio(pid: int, db: Session = Depends(get_db)):
    p = db.query(PortfolioStrategy).filter(PortfolioStrategy.id == pid).first()
    if not p:
        return {"code": 404, "message": "组合策略不存在"}
    try:
        db.delete(p)  # Strategy 随 ondelete=CASCADE 删
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"code": 409, "message": "该组合策略被回测记录或实盘 session 引用，无法删除"}
    return {"code": 0, "data": None}


# ===========================================================================
# 独立子策略 CRUD — /api/portfolios/{pid}/strategies
# 两层设计：组合与子策略分开管理。此组端点管理单个子策略。
# slave 的 master_strategy_id 是已存在的同组合 master 的 strategy id（正整数）。
# ===========================================================================
class StrategyCreate(BaseModel):
    name: str
    formula_id: int
    period: str
    role: str
    master_strategy_id: int | None = None  # 已存在的同组合 master id
    capital_ratio: float = 0.6
    max_positions: int = 5
    single_open_ratio: float = 0.1
    stop_loss_ratio: float = 0.05
    take_profit_ratio: float = 0.15
    trailing_stop_ratio: float = 0.03
    add_position_threshold: float = 0.05
    max_add_count: int = 2
    add_position_ratio: float = 0.1
    reduce_position_ratio: float = 0.3


def _apply_strategy_fields(s: Strategy, req: StrategyCreate) -> None:
    s.name = req.name
    s.formula_id = req.formula_id
    s.period = req.period
    s.role = req.role
    s.master_strategy_id = req.master_strategy_id
    s.capital_ratio = req.capital_ratio
    s.max_positions = req.max_positions
    s.single_open_ratio = req.single_open_ratio
    s.stop_loss_ratio = req.stop_loss_ratio
    s.take_profit_ratio = req.take_profit_ratio
    s.trailing_stop_ratio = req.trailing_stop_ratio
    s.add_position_threshold = req.add_position_threshold
    s.max_add_count = req.max_add_count
    s.add_position_ratio = req.add_position_ratio
    s.reduce_position_ratio = req.reduce_position_ratio


def _validate_strategy(req: StrategyCreate, db: Session, pid: int) -> str | None:
    """单个子策略校验。master_strategy_id 必须是同组合下 role=master 的已存在 strategy。"""
    if req.role not in VALID_ROLES:
        return f"role 必须为 {sorted(VALID_ROLES)}，收到 {req.role}"
    if req.period not in VALID_PERIODS:
        return f"period 必须为 {sorted(VALID_PERIODS)}，收到 {req.period}"
    if not (0 < req.capital_ratio <= 1):
        return f"capital_ratio 必须在 (0,1]，收到 {req.capital_ratio}"
    if not db.query(Formula).filter(Formula.id == req.formula_id).first():
        return f"公式 id={req.formula_id} 不存在"
    if req.role == "slave":
        if req.master_strategy_id is None:
            return "role=slave 必须指定主策略（master_strategy_id）"
        # master 必须是同组合下 role=master 的已存在策略
        master = db.query(Strategy).filter(Strategy.id == req.master_strategy_id).first()
        if not master:
            return f"主策略 id={req.master_strategy_id} 不存在"
        if master.portfolio_id != pid:
            return "主策略不属于本组合"
        if master.role != "master":
            return "主策略角色必须为 master"
    else:  # master / independent
        if req.master_strategy_id is not None:
            return f"role={req.role} 的 master_strategy_id 必须为空"
    return None


@router.get("/{pid}/strategies")
def list_strategies(pid: int, db: Session = Depends(get_db)):
    if not db.query(PortfolioStrategy).filter(PortfolioStrategy.id == pid).first():
        return {"code": 404, "message": "组合策略不存在"}
    items = db.query(Strategy).filter(Strategy.portfolio_id == pid).order_by(Strategy.id).all()
    return {"code": 0, "data": [_serialize_strategy(s) for s in items]}


@router.post("/{pid}/strategies")
def create_strategy(pid: int, req: StrategyCreate, db: Session = Depends(get_db)):
    if not db.query(PortfolioStrategy).filter(PortfolioStrategy.id == pid).first():
        return {"code": 404, "message": "组合策略不存在"}
    err = _validate_strategy(req, db, pid)
    if err:
        return {"code": 400, "message": err}
    s = Strategy(portfolio_id=pid)
    _apply_strategy_fields(s, req)
    db.add(s)
    db.commit()
    db.refresh(s)
    return {"code": 0, "data": _serialize_strategy(s)}


@router.put("/{pid}/strategies/{sid}")
def update_strategy(pid: int, sid: int, req: StrategyCreate, db: Session = Depends(get_db)):
    s = db.query(Strategy).filter(Strategy.id == sid, Strategy.portfolio_id == pid).first()
    if not s:
        return {"code": 404, "message": "子策略不存在"}
    err = _validate_strategy(req, db, pid)
    if err:
        return {"code": 400, "message": err}
    _apply_strategy_fields(s, req)
    db.commit()
    db.refresh(s)
    return {"code": 0, "data": _serialize_strategy(s)}


@router.delete("/{pid}/strategies/{sid}")
def delete_strategy(pid: int, sid: int, db: Session = Depends(get_db)):
    s = db.query(Strategy).filter(Strategy.id == sid, Strategy.portfolio_id == pid).first()
    if not s:
        return {"code": 404, "message": "子策略不存在"}
    # 删 master 前 check 无 slave 引用
    if s.role == "master":
        slave_count = db.query(Strategy).filter(Strategy.master_strategy_id == sid).count()
        if slave_count > 0:
            return {"code": 400, "message": "该主策略被从策略引用，无法删除"}
    # 子策略可能被 backtest_trades / live_trades / live_orders 引用（历史交易记录，
    # strategy_id FK 默认 RESTRICT）。直删触发 IntegrityError → 拦截返回可读错误，
    # 不破坏历史数据；用户需先删相关回测/实盘记录才能删该子策略。
    try:
        db.delete(s)
        db.commit()
    except IntegrityError:
        db.rollback()
        return {
            "code": 400,
            "message": "该子策略被回测或实盘交易记录引用，无法删除。请先删除相关的回测记录或实盘会话。",
        }
    return {"code": 0, "data": None}
