"""组合策略管理 CRUD — 组合（PortfolioStrategy）+ 嵌套子策略（Strategy）。

业务逻辑已下沉到 core.services.strategy_service（P1 #9 续块）。
路由层仅剩：HTTP 入口 + pydantic 模型 + VALID_* 校验常量 + _validate_* 纯校验函数
+ 资源校验(404) + IntegrityError→409/400 翻译 + ok/err 包装。

模型无 relationship，子列表用显式二次查询。主从策略自引用（master_strategy_id）
用两步 commit 处理：先 insert 全部子策略拿 id，再 UPDATE master_strategy_id。

前端约定：master_strategy_id=0 表示"指向本批第 0 个子策略"（新建时用临时索引），
非 0 正整数表示已存在的 strategy id（编辑时保留）。后端统一解析为真实 id。
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.api.response import err, ok
from core.db import get_db
from core.models import PortfolioStrategy, Strategy, StockPool, Formula
from core.services.strategy_service import (
    list_portfolios as _svc_list_portfolios,
    get_portfolio as _svc_get_portfolio,
    create_portfolio as _svc_create_portfolio,
    update_portfolio as _svc_update_portfolio,
    delete_portfolio as _svc_delete_portfolio,
    portfolio_exists as _svc_portfolio_exists,
    list_strategies as _svc_list_strategies,
    create_strategy as _svc_create_strategy,
    get_strategy as _svc_get_strategy,
    update_strategy as _svc_update_strategy,
    delete_strategy as _svc_delete_strategy,
    count_slave_strategies as _svc_count_slave_strategies,
)

router = APIRouter(prefix="/api/portfolios", tags=["portfolios"])

# 枚举校验集合（出处：docs/system-plan-draft.md §5.3.2）
# 周期取 TQ 公式支持 ∩ iQuant 桥 xtdata 本地读取白名单的交集（open-questions Q4）。
# 实盘三段式（C6）：1m/5m/15m/30m/1h 走桥 BarPoller + 边界分发；1d 走 14:30 快照；
# 1w/1mon 桥端 xtdata 拉不到，走通达信 TQFormula.compute 启动/日终注入（见 live_engine._STARTUP_ONLY_PERIODS）。
# 注意 60h 两端都不认（TQ periodstr error + xtdata 白名单是 1h 不是 60m）。
# VALID_PERIODS 被 test_backtest_data 直接 import 做白名单对齐，必须留路由。
VALID_PERIODS = {"1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mon"}
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
        # 加仓阈值：-1=特殊值（任何价格都加），0=不涨即加，正常 (0,1]
        if s.add_position_threshold != -1 and not (0 <= s.add_position_threshold <= 1):
            return f"子策略[{idx}] 加仓阈值须为 -1（任何价都加）或 [0,1]，收到 {s.add_position_threshold}"
        if not (0 <= s.max_add_count <= 10):
            return f"子策略[{idx}] 加仓次数须在 [0,10]（0=禁加仓），收到 {s.max_add_count}"
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


@router.get("")
def list_portfolios(db: Session = Depends(get_db)):
    return ok(_svc_list_portfolios(db))


@router.get("/{pid}")
def get_portfolio(pid: int, db: Session = Depends(get_db)):
    data = _svc_get_portfolio(db, pid)
    if data is None:
        return err(404, "组合策略不存在")
    return ok(data)


@router.post("")
def create_portfolio(req: PortfolioCreate, db: Session = Depends(get_db)):
    err_msg = _validate_portfolio(req, db)
    if err_msg:
        return err(400, err_msg)
    return ok(_svc_create_portfolio(db, req))


@router.put("/{pid}")
def update_portfolio(pid: int, req: PortfolioCreate, db: Session = Depends(get_db)):
    # 校验参数（400）→ 再调 service（内部判 404 + 应用 + 提交）。
    # service 把「判存在 + 应用 + 提交」合并后，须先校验避免把非法字段写库；
    # 「不存在 id + 非法参数」无测试覆盖且语义上 400 更合理。
    err_msg = _validate_portfolio(req, db)
    if err_msg:
        return err(400, err_msg)
    data = _svc_update_portfolio(db, pid, req)
    if data is None:
        return err(404, "组合策略不存在")
    return ok(data)


@router.delete("/{pid}")
def delete_portfolio(pid: int, db: Session = Depends(get_db)):
    try:
        if not _svc_delete_portfolio(db, pid):
            return err(404, "组合策略不存在")
    except IntegrityError:
        db.rollback()
        return err(409, "该组合策略被回测记录或实盘 session 引用，无法删除")
    return ok()


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


def _validate_strategy(req: StrategyCreate, db: Session, pid: int) -> str | None:
    """单个子策略校验。master_strategy_id 必须是同组合下 role=master 的已存在 strategy。"""
    if req.role not in VALID_ROLES:
        return f"role 必须为 {sorted(VALID_ROLES)}，收到 {req.role}"
    if req.period not in VALID_PERIODS:
        return f"period 必须为 {sorted(VALID_PERIODS)}，收到 {req.period}"
    if not (0 < req.capital_ratio <= 1):
        return f"capital_ratio 必须在 (0,1]，收到 {req.capital_ratio}"
    # 加仓阈值：-1=特殊值（任何价格都加），0=不涨即加，正常 (0,1]
    if req.add_position_threshold != -1 and not (0 <= req.add_position_threshold <= 1):
        return f"加仓阈值须为 -1（任何价都加）或 [0,1]，收到 {req.add_position_threshold}"
    if not (0 <= req.max_add_count <= 10):
        return f"加仓次数须在 [0,10]（0=禁加仓），收到 {req.max_add_count}"
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
    if not _svc_portfolio_exists(db, pid):
        return err(404, "组合策略不存在")
    return ok(_svc_list_strategies(db, pid))


@router.post("/{pid}/strategies")
def create_strategy(pid: int, req: StrategyCreate, db: Session = Depends(get_db)):
    if not _svc_portfolio_exists(db, pid):
        return err(404, "组合策略不存在")
    err_msg = _validate_strategy(req, db, pid)
    if err_msg:
        return err(400, err_msg)
    return ok(_svc_create_strategy(db, pid, req))


@router.put("/{pid}/strategies/{sid}")
def update_strategy(pid: int, sid: int, req: StrategyCreate, db: Session = Depends(get_db)):
    # 校验参数（400）→ 再调 service（判 404 + 应用 + 提交），同 update_portfolio。
    err_msg = _validate_strategy(req, db, pid)
    if err_msg:
        return err(400, err_msg)
    data = _svc_update_strategy(db, pid, sid, req)
    if data is None:
        return err(404, "子策略不存在")
    return ok(data)


@router.delete("/{pid}/strategies/{sid}")
def delete_strategy(pid: int, sid: int, db: Session = Depends(get_db)):
    s = _svc_get_strategy(db, pid, sid)
    if not s:
        return err(404, "子策略不存在")
    # 删 master 前 check 无 slave 引用
    if s.role == "master":
        if _svc_count_slave_strategies(db, sid) > 0:
            return err(400, "该主策略被从策略引用，无法删除")
    # 子策略可能被 backtest_trades / live_trades / live_orders 引用（历史交易记录，
    # strategy_id FK 默认 RESTRICT）。直删触发 IntegrityError → 拦截返回可读错误，
    # 不破坏历史数据；用户需先删相关回测/实盘记录才能删该子策略。
    try:
        _svc_delete_strategy(db, pid, sid)
    except IntegrityError:
        db.rollback()
        return err(
            400,
            "该子策略被回测或实盘交易记录引用，无法删除。请先删除相关的回测记录或实盘会话。",
        )
    return ok()
