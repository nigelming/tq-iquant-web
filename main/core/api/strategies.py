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
from core.models import PortfolioStrategy, Strategy, StockPool, StockPoolStock, Formula
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
VALID_SLAVE_EXIT_MODES = {"independent", "follow_master"}
VALID_TRADING_SESSIONS = {"full", "am", "pm"}  # draft:499，非 morning/afternoon
VALID_STATUSES = {"active", "archived"}  # draft:500
VALID_POSITION_SIZING = {"dynamic", "fixed"}  # W2 仓位模型：动态净资产/固定名义


class StrategyItem(BaseModel):
    name: str
    formula_id: int
    period: str
    role: str
    master_strategy_id: int | None = None  # 0=本批第N个；正整数=已存在 id；None=无
    stock_pool_id: int | None = None  # None=继承组合级
    slave_exit_mode: str = "independent"  # independent|follow_master（仅 slave 有意义）
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
    # B4 OPEN 择优轮动（默认关）：满槽时强信号轮出最弱持仓换入新信号股
    rotation_enabled: bool = False
    rotation_momentum_bars: int = 20
    rotation_hysteresis: float = 0.05
    # B5 市场状态闸（默认关）：基准周线 bull 态用 trend_* 覆盖参数
    regime_filter_enabled: bool = False
    trend_trailing_stop_ratio: float | None = None
    trend_max_positions: int | None = None
    # #3 策略级敞口上限（默认关）：回测 BUY 审批卡策略总持仓市值 ≤ capital_ratio × 初始资金
    exposure_cap_enabled: bool = False


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
    # #1 涨跌停约束（回测）：None=新建默认开启/编辑不动；显式 bool=按值设置
    price_limit_check_enabled: bool | None = None
    # #2 成交量/流动性约束（回测，默认关）：None=不动（新建落默认 False/0.10）
    volume_limit_enabled: bool | None = None
    volume_limit_ratio: float | None = None
    # #4 自适应滑点（回测，默认关）：None=不动（新建落默认 False/0.10）
    adaptive_slippage_enabled: bool | None = None
    slippage_impact_coef: float | None = None
    # W2 仓位模型：dynamic=动态净资产（默认）/ fixed=固定名义
    position_sizing: str = "dynamic"
    # W2 现金管理增强（预留，默认关）：命中任一阈值记决策事件（不自动下单）
    cash_enhance_enabled: bool = False
    cash_enhance_equity_threshold: float | None = None
    cash_enhance_position_ratio_threshold: float | None = None
    # W3 容量上限（可选，默认 None）：dynamic 仓位资金封顶，超净值部分闲置（撤回/封盘）
    equity_cap: float | None = None
    status: str = "active"
    strategies: list[StrategyItem] = []


def _effective_pool_codes(
    db: Session, portfolio_pool_id: int | None, strategy_pool_id: int | None
) -> set[str]:
    """策略有效股票池成分：strategy_pool_id 非空用独立池，否则继承组合级。"""
    pool_id = strategy_pool_id if strategy_pool_id is not None else portfolio_pool_id
    if pool_id is None:
        return set()
    return {
        row.stock_code
        for row in db.query(StockPoolStock).filter_by(pool_id=pool_id).all()
    }


def _check_slave_pool_subset(
    db: Session,
    portfolio_pool_id: int | None,
    slave_pool_id: int | None,
    master_pool_id: int | None,
    label: str = "",
) -> str | None:
    """从策略有效池必须 ⊆ 主策略有效池。

    否则该股 master 永远不会持有 → slave 的 OPEN 被主从闸门永久拦截，属静默失效；
    配置期显式拒绝。返回错误消息或 None（通过）。
    """
    slave_codes = _effective_pool_codes(db, portfolio_pool_id, slave_pool_id)
    master_codes = _effective_pool_codes(db, portfolio_pool_id, master_pool_id)
    extra = slave_codes - master_codes
    if extra:
        preview = "、".join(sorted(extra)[:5])
        return (
            "%s从策略股票池必须包含于主策略股票池，越界 %d 只：%s"
            % (label, len(extra), preview)
        )
    return None


def _check_portfolio_slaves_subset(
    db: Session, pid: int, portfolio_pool_id: int | None
) -> str | None:
    """组合级池变更后，重校该组合内所有 (slave, master) 对的池子集约束。"""
    strategies = db.query(Strategy).filter(Strategy.portfolio_id == pid).all()
    by_id = {s.id: s for s in strategies}
    for s in strategies:
        if s.role != "slave" or s.master_strategy_id is None:
            continue
        master = by_id.get(s.master_strategy_id)
        if master is None:
            continue
        msg = _check_slave_pool_subset(
            db, portfolio_pool_id, s.stock_pool_id, master.stock_pool_id,
            label=f"从策略 {s.name} ",
        )
        if msg:
            return msg
    return None


def _validate_portfolio(req: PortfolioCreate, db: Session) -> str | None:
    """返回错误消息（str）或 None（校验通过）。"""
    if not req.name.strip():
        return "组合名称不能为空"
    if req.trading_session not in VALID_TRADING_SESSIONS:
        return f"trading_session 必须为 {sorted(VALID_TRADING_SESSIONS)}，收到 {req.trading_session}"
    if req.status not in VALID_STATUSES:
        return f"status 必须为 {sorted(VALID_STATUSES)}，收到 {req.status}"
    if not (0 < req.max_drawdown < 1):
        return f"max_drawdown 必须在 (0,1)，收到 {req.max_drawdown}"
    if not (0 < req.daily_loss_limit < 1):
        return f"daily_loss_limit 必须在 (0,1)，收到 {req.daily_loss_limit}"
    if req.volume_limit_ratio is not None and not (0 < req.volume_limit_ratio <= 1):
        return f"volume_limit_ratio 必须在 (0,1]，收到 {req.volume_limit_ratio}"
    if req.slippage_impact_coef is not None and not (0 < req.slippage_impact_coef <= 1):
        return f"slippage_impact_coef 必须在 (0,1]，收到 {req.slippage_impact_coef}"
    if req.position_sizing not in VALID_POSITION_SIZING:
        return f"position_sizing 必须为 {sorted(VALID_POSITION_SIZING)}，收到 {req.position_sizing}"
    if req.cash_enhance_position_ratio_threshold is not None and not (
        0 < req.cash_enhance_position_ratio_threshold <= 1
    ):
        return (
            f"cash_enhance_position_ratio_threshold 必须在 (0,1]，"
            f"收到 {req.cash_enhance_position_ratio_threshold}"
        )
    if req.equity_cap is not None and req.equity_cap <= 0:
        return f"equity_cap 必须 > 0（None=不设上限），收到 {req.equity_cap}"
    if not db.query(StockPool).filter(StockPool.id == req.stock_pool_id).first():
        return f"股票池 id={req.stock_pool_id} 不存在"
    # 子策略校验
    for idx, s in enumerate(req.strategies):
        if not s.name.strip():
            return f"子策略[{idx}] 名称不能为空"
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
            if s.master_strategy_id < 0:
                return f"子策略[{idx}] master_strategy_id 非法：负数（正数=本批索引）"
            if s.slave_exit_mode not in VALID_SLAVE_EXIT_MODES:
                return f"子策略[{idx}] slave_exit_mode 必须为 {sorted(VALID_SLAVE_EXIT_MODES)}，收到 {s.slave_exit_mode}"
        else:  # master / independent
            if s.master_strategy_id is not None:
                return f"子策略[{idx}] role={s.role} 的 master_strategy_id 必须为空"
            if s.slave_exit_mode != "independent":
                return f"子策略[{idx}] role={s.role} 的 slave_exit_mode 必须为 independent"
        # 策略级股票池校验
        if s.stock_pool_id is not None and not db.query(StockPool).filter(StockPool.id == s.stock_pool_id).first():
            return f"子策略[{idx}] 股票池 id={s.stock_pool_id} 不存在"
    # slave 的 master 指向必须是同批 master 角色（master_strategy_id=0..N 指本批索引）
    for idx, s in enumerate(req.strategies):
        if s.role == "slave" and s.master_strategy_id is not None and s.master_strategy_id >= 0:
            target_idx = s.master_strategy_id
            if target_idx >= len(req.strategies):
                return f"子策略[{idx}] 主策略索引 {target_idx} 超出范围"
            target = req.strategies[target_idx]
            if target.role != "master":
                return f"子策略[{idx}] 指向的主策略[{target_idx}] 角色为 {target.role}，必须为 master"
    # slave 池 ⊆ master 池（越界 → 该股 slave 永久无法开仓，静默失效）
    for idx, s in enumerate(req.strategies):
        if s.role == "slave" and s.master_strategy_id is not None and 0 <= s.master_strategy_id < len(req.strategies):
            target = req.strategies[s.master_strategy_id]
            msg = _check_slave_pool_subset(
                db, req.stock_pool_id, s.stock_pool_id, target.stock_pool_id,
                label=f"子策略[{idx}] ",
            )
            if msg:
                return msg
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
    # 组合级池变更会影响 slave 继承的有效池 → 重校既有主从对的子集约束。
    sub_msg = _check_portfolio_slaves_subset(db, pid, req.stock_pool_id)
    if sub_msg:
        return err(400, sub_msg)
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
    stock_pool_id: int | None = None  # None=继承组合级
    slave_exit_mode: str = "independent"  # independent|follow_master（仅 slave 有意义）
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
    # B4 OPEN 择优轮动（默认关）：满槽时强信号轮出最弱持仓换入新信号股
    rotation_enabled: bool = False
    rotation_momentum_bars: int = 20
    rotation_hysteresis: float = 0.05
    # B5 市场状态闸（默认关）：基准周线 bull 态用 trend_* 覆盖参数
    regime_filter_enabled: bool = False
    trend_trailing_stop_ratio: float | None = None
    trend_max_positions: int | None = None
    # #3 策略级敞口上限（默认关）：回测 BUY 审批卡策略总持仓市值 ≤ capital_ratio × 初始资金
    exposure_cap_enabled: bool = False


def _validate_strategy(
    req: StrategyCreate, db: Session, pid: int, sid: int | None = None
) -> str | None:
    """单个子策略校验。master_strategy_id 必须是同组合下 role=master 的已存在 strategy。

    sid 非空（编辑场景）：额外校验"改主策略/降级"对既有主从关系的影响。
    """
    if not req.name.strip():
        return "策略名称不能为空"
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
    # B4/B5 可选增强字段校验（仅在取值越界时拒绝，默认关不校验 trend_*）
    if not (1 <= req.rotation_momentum_bars <= 250):
        return f"rotation_momentum_bars 须在 [1,250]，收到 {req.rotation_momentum_bars}"
    if not (0 <= req.rotation_hysteresis <= 1):
        return f"rotation_hysteresis 须在 [0,1]，收到 {req.rotation_hysteresis}"
    if req.trend_trailing_stop_ratio is not None and not (0 < req.trend_trailing_stop_ratio <= 1):
        return f"trend_trailing_stop_ratio 须在 (0,1]，收到 {req.trend_trailing_stop_ratio}"
    if req.trend_max_positions is not None and req.trend_max_positions < 1:
        return f"trend_max_positions 须 ≥ 1，收到 {req.trend_max_positions}"
    if not db.query(Formula).filter(Formula.id == req.formula_id).first():
        return f"公式 id={req.formula_id} 不存在"
    if req.role == "slave":
        if req.master_strategy_id is None:
            return "role=slave 必须指定主策略（master_strategy_id）"
        if req.slave_exit_mode not in VALID_SLAVE_EXIT_MODES:
            return f"slave_exit_mode 必须为 {sorted(VALID_SLAVE_EXIT_MODES)}，收到 {req.slave_exit_mode}"
        # master 必须是同组合下 role=master 的已存在策略
        master = db.query(Strategy).filter(Strategy.id == req.master_strategy_id).first()
        if not master:
            return f"主策略 id={req.master_strategy_id} 不存在"
        if master.portfolio_id != pid:
            return "主策略不属于本组合"
        if sid is not None and req.master_strategy_id == sid:
            return "主策略不能指向自身"
        if master.role != "master":
            return "主策略角色必须为 master"
        # slave 有效池必须 ⊆ master 有效池（越界 → 该股永久无法开仓）
        ps = db.query(PortfolioStrategy).filter(PortfolioStrategy.id == pid).first()
        portfolio_pool_id = ps.stock_pool_id if ps else None
        msg = _check_slave_pool_subset(
            db, portfolio_pool_id, req.stock_pool_id, master.stock_pool_id,
        )
        if msg:
            return msg
    else:  # master / independent
        if req.master_strategy_id is not None:
            return f"role={req.role} 的 master_strategy_id 必须为空"
        if req.slave_exit_mode != "independent":
            return f"role={req.role} 的 slave_exit_mode 必须为 independent"
    # 策略级股票池校验
    if req.stock_pool_id is not None and not db.query(StockPool).filter(StockPool.id == req.stock_pool_id).first():
        return f"股票池 id={req.stock_pool_id} 不存在"
    # 编辑场景：改主策略 / 降级对既有主从关系的影响
    if sid is not None:
        slaves = db.query(Strategy).filter(Strategy.master_strategy_id == sid).all()
        if slaves and req.role != "master":
            return "该主策略被从策略引用，role 不能改为 %s" % req.role
        # 编辑的是 master：其股票池变更后，各 slave 有效池仍须 ⊆ 新 master 池
        if req.role == "master":
            ps = db.query(PortfolioStrategy).filter(PortfolioStrategy.id == pid).first()
            portfolio_pool_id = ps.stock_pool_id if ps else None
            for slave in slaves:
                msg = _check_slave_pool_subset(
                    db, portfolio_pool_id, slave.stock_pool_id, req.stock_pool_id,
                    label=f"从策略 {slave.name} ",
                )
                if msg:
                    return msg
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
    err_msg = _validate_strategy(req, db, pid, sid)
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
