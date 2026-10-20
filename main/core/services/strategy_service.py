"""组合策略 Service 层（P1 #9 续块）。

承接 core.api.strategies 的业务逻辑：组合/子策略序列化、CRUD、
两步 commit（先 insert 全部子策略拿 id 再 UPDATE master_strategy_id）、
主从策略自引用处理、删除（含 RESTRICT 引用拦截）。

校验（_validate_portfolio/_validate_strategy + VALID_* 常量）留路由（HTTP 400 语义），
其中 VALID_PERIODS 被 test_backtest_data 直接 import 做白名单对齐，必须留路由。
删 master 前的 slave 引用计数检查也留路由（HTTP 400 语义 + 业务规则）。

异常约定：
- IntegrityError：被回测/实盘记录引用（ondelete=RESTRICT）→ 路由 err(400/409)

路由层仅剩 HTTP 入口 + 资源校验(404) + 校验(400) + IntegrityError 翻译 + ok/err 包装。
"""
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.models import PortfolioStrategy, Strategy


def serialize_strategy(s: Strategy) -> dict:
    return {
        "id": s.id,
        "portfolio_id": s.portfolio_id,
        "name": s.name,
        "formula_id": s.formula_id,
        "period": s.period,
        "role": s.role,
        "master_strategy_id": s.master_strategy_id,
        "stock_pool_id": s.stock_pool_id,
        "slave_exit_mode": s.slave_exit_mode,
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
        # B4/B5 可选增强（默认关）
        "rotation_enabled": bool(s.rotation_enabled) if s.rotation_enabled is not None else False,
        "rotation_momentum_bars": s.rotation_momentum_bars if s.rotation_momentum_bars is not None else 20,
        "rotation_hysteresis": float(s.rotation_hysteresis) if s.rotation_hysteresis is not None else 0.05,
        "regime_filter_enabled": bool(s.regime_filter_enabled) if s.regime_filter_enabled is not None else False,
        "trend_trailing_stop_ratio": float(s.trend_trailing_stop_ratio) if s.trend_trailing_stop_ratio is not None else None,
        "trend_max_positions": s.trend_max_positions,
        # #3 策略级敞口上限（默认关）
        "exposure_cap_enabled": bool(s.exposure_cap_enabled) if s.exposure_cap_enabled is not None else False,
    }


def serialize_portfolio(db: Session, p: PortfolioStrategy) -> dict:
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
        "price_limit_check_enabled": bool(p.price_limit_check_enabled) if p.price_limit_check_enabled is not None else False,
        "volume_limit_enabled": bool(p.volume_limit_enabled) if p.volume_limit_enabled is not None else False,
        "volume_limit_ratio": float(p.volume_limit_ratio) if p.volume_limit_ratio is not None else 0.10,
        "adaptive_slippage_enabled": bool(p.adaptive_slippage_enabled) if p.adaptive_slippage_enabled is not None else False,
        "slippage_impact_coef": float(p.slippage_impact_coef) if p.slippage_impact_coef is not None else 0.10,
        "position_sizing": p.position_sizing or "dynamic",
        "cash_enhance_enabled": bool(p.cash_enhance_enabled) if p.cash_enhance_enabled is not None else False,
        "cash_enhance_equity_threshold": (
            float(p.cash_enhance_equity_threshold) if p.cash_enhance_equity_threshold is not None else None
        ),
        "cash_enhance_position_ratio_threshold": (
            float(p.cash_enhance_position_ratio_threshold)
            if p.cash_enhance_position_ratio_threshold is not None else None
        ),
        "equity_cap": (float(p.equity_cap) if p.equity_cap is not None else None),
        "status": p.status,
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        "strategies": [serialize_strategy(s) for s in subs],
    }


def _apply_portfolio_fields(p: PortfolioStrategy, req) -> None:
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
    # #1 涨跌停约束：None（请求未携带）不动已有值——编辑旧组合不偷改开关；
    # 新建默认 True 在 create_portfolio 兜底
    if getattr(req, "price_limit_check_enabled", None) is not None:
        p.price_limit_check_enabled = req.price_limit_check_enabled
    # #2 成交量约束（默认关）：None 不动（新建落 ORM 默认 False/0.10）
    if getattr(req, "volume_limit_enabled", None) is not None:
        p.volume_limit_enabled = req.volume_limit_enabled
    if getattr(req, "volume_limit_ratio", None) is not None:
        p.volume_limit_ratio = req.volume_limit_ratio
    # #4 自适应滑点（默认关）：None 不动（新建落 ORM 默认 False/0.10）
    if getattr(req, "adaptive_slippage_enabled", None) is not None:
        p.adaptive_slippage_enabled = req.adaptive_slippage_enabled
    if getattr(req, "slippage_impact_coef", None) is not None:
        p.slippage_impact_coef = req.slippage_impact_coef
    # W2 仓位模型 + 现金管理（请求携带默认值，按值落库）
    if getattr(req, "position_sizing", None) is not None:
        p.position_sizing = req.position_sizing
    if getattr(req, "cash_enhance_enabled", None) is not None:
        p.cash_enhance_enabled = req.cash_enhance_enabled
    p.cash_enhance_equity_threshold = getattr(req, "cash_enhance_equity_threshold", None)
    p.cash_enhance_position_ratio_threshold = getattr(
        req, "cash_enhance_position_ratio_threshold", None)
    # W3 容量上限（可选，None=不设上限）
    p.equity_cap = getattr(req, "equity_cap", None)


def _create_strategies_two_step(db: Session, pid: int, strategies: list) -> None:
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
            stock_pool_id=s.stock_pool_id, slave_exit_mode=s.slave_exit_mode,
            rotation_enabled=getattr(s, "rotation_enabled", False),
            rotation_momentum_bars=getattr(s, "rotation_momentum_bars", 20),
            rotation_hysteresis=getattr(s, "rotation_hysteresis", 0.05),
            regime_filter_enabled=getattr(s, "regime_filter_enabled", False),
            trend_trailing_stop_ratio=getattr(s, "trend_trailing_stop_ratio", None),
            trend_max_positions=getattr(s, "trend_max_positions", None),
            exposure_cap_enabled=getattr(s, "exposure_cap_enabled", False),
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


# ===========================================================================
# 组合 CRUD
# ===========================================================================
def list_portfolios(db: Session) -> list[dict]:
    items = db.query(PortfolioStrategy).order_by(PortfolioStrategy.id).all()
    return [serialize_portfolio(db, p) for p in items]


def get_portfolio(db: Session, pid: int) -> dict | None:
    """返回组合详情或 None（不存在）。"""
    p = db.query(PortfolioStrategy).filter(PortfolioStrategy.id == pid).first()
    if p is None:
        return None
    return serialize_portfolio(db, p)


def create_portfolio(db: Session, req) -> dict:
    """建组合 + 子策略（两步 commit）。req 已校验合法。"""
    p = PortfolioStrategy()
    _apply_portfolio_fields(p, req)
    # #1 新组合默认开启涨跌停约束（旧组合迁移后 server_default '0' 保持关闭）
    if getattr(req, "price_limit_check_enabled", None) is None:
        p.price_limit_check_enabled = True
    db.add(p)
    db.flush()
    _create_strategies_two_step(db, p.id, req.strategies)
    db.commit()
    db.refresh(p)
    return serialize_portfolio(db, p)


def update_portfolio(db: Session, pid: int, req) -> dict | None:
    """更新组合字段（name/资金/费率/风控等），不动子策略。None=不存在。req 已校验合法。

    子策略不在此全量替换：前端编辑弹窗只发 strategies: []（全量替换曾把子策略
    清空，2026-09 实测）；且删旧建新会换 strategy id，破坏 backtest_trades 等历史
    引用。子策略增删改走独立端点 POST/PUT/DELETE /api/portfolios/{pid}/strategies。
    req.strategies 在此被忽略（前端编辑弹窗恒传 []）。
    """
    p = db.query(PortfolioStrategy).filter(PortfolioStrategy.id == pid).first()
    if p is None:
        return None
    _apply_portfolio_fields(p, req)
    db.commit()
    db.refresh(p)
    return serialize_portfolio(db, p)


def delete_portfolio(db: Session, pid: int) -> bool:
    """删组合（Strategy 随 ondelete=CASCADE 删）。

    False=不存在；被回测记录/实盘 session 引用时 ondelete=RESTRICT 抛 IntegrityError（路由 catch→409）。

    删除前先解除组合内子策略的主从自引用（strategies.master_strategy_id 亦为 RESTRICT）：
    否则级联删子策略时先删 master 会因仍被 slave 引用触发 IntegrityError，被路由误报为
    "被回测记录或实盘 session 引用"。master_strategy_id 只可能指向同组合策略，故置空安全。
    """
    p = db.query(PortfolioStrategy).filter(PortfolioStrategy.id == pid).first()
    if p is None:
        return False
    db.query(Strategy).filter(Strategy.portfolio_id == pid).update(
        {Strategy.master_strategy_id: None}, synchronize_session=False
    )
    db.delete(p)
    db.commit()
    return True


# ===========================================================================
# 独立子策略 CRUD
# ===========================================================================
def _apply_strategy_fields(s: Strategy, req) -> None:
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
    s.stock_pool_id = req.stock_pool_id
    s.slave_exit_mode = req.slave_exit_mode
    # B4/B5 可选增强（默认关；缺省字段回落默认，保持旧请求行为不变）
    s.rotation_enabled = getattr(req, "rotation_enabled", False)
    s.rotation_momentum_bars = getattr(req, "rotation_momentum_bars", 20)
    s.rotation_hysteresis = getattr(req, "rotation_hysteresis", 0.05)
    s.regime_filter_enabled = getattr(req, "regime_filter_enabled", False)
    s.trend_trailing_stop_ratio = getattr(req, "trend_trailing_stop_ratio", None)
    s.trend_max_positions = getattr(req, "trend_max_positions", None)
    # #3 策略级敞口上限（默认关；缺省字段回落 False，保持旧请求行为不变）
    s.exposure_cap_enabled = getattr(req, "exposure_cap_enabled", False)


def portfolio_exists(db: Session, pid: int) -> bool:
    """组合是否存在（路由层 404 校验用）。"""
    return db.query(PortfolioStrategy).filter(PortfolioStrategy.id == pid).first() is not None


def list_strategies(db: Session, pid: int) -> list[dict]:
    """列出组合下子策略（调用方应先 portfolio_exists 判 404）。"""
    items = db.query(Strategy).filter(Strategy.portfolio_id == pid).order_by(Strategy.id).all()
    return [serialize_strategy(s) for s in items]


def create_strategy(db: Session, pid: int, req) -> dict:
    """建单个子策略。req 已校验合法，组合已确认存在。"""
    s = Strategy(portfolio_id=pid)
    _apply_strategy_fields(s, req)
    db.add(s)
    db.commit()
    db.refresh(s)
    return serialize_strategy(s)


def get_strategy(db: Session, pid: int, sid: int) -> Strategy | None:
    """查子策略（路由层 404 校验 + delete 主从检查用）。"""
    return db.query(Strategy).filter(Strategy.id == sid, Strategy.portfolio_id == pid).first()


def update_strategy(db: Session, pid: int, sid: int, req) -> dict | None:
    """更新单个子策略。None=不存在。req 已校验合法。"""
    s = db.query(Strategy).filter(Strategy.id == sid, Strategy.portfolio_id == pid).first()
    if s is None:
        return None
    _apply_strategy_fields(s, req)
    db.commit()
    db.refresh(s)
    return serialize_strategy(s)


def delete_strategy(db: Session, pid: int, sid: int) -> bool:
    """删子策略。

    False=不存在；被回测/实盘交易记录引用时 strategy_id FK RESTRICT 抛 IntegrityError
    （路由 catch→400）。master 的 slave 引用计数检查由路由层做（业务规则 400）。
    """
    s = db.query(Strategy).filter(Strategy.id == sid, Strategy.portfolio_id == pid).first()
    if s is None:
        return False
    db.delete(s)
    db.commit()
    return True


def count_slave_strategies(db: Session, master_sid: int) -> int:
    """统计指向 master_sid 的 slave 数量（路由删 master 前 400 校验用）。"""
    return db.query(Strategy).filter(Strategy.master_strategy_id == master_sid).count()
