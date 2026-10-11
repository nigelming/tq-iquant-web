"""Portfolio 组装工具（回测/实盘共用）。

从 DB 的 PortfolioStrategy + Strategy + FormulaSignal 行组装引擎层 Portfolio 对象。
回测（BacktestEngine）与实盘（LiveEngine）共用此逻辑，保证两路持仓/风控/信号配置一致。
"""
from decimal import Decimal
from typing import List

from sqlalchemy.orm import Session

from core.models import PortfolioStrategy, Strategy, FormulaSignal, StockPoolStock
from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import PortfolioRiskManager, StrategyRiskManager
from tq_iquant_shared.constants import SignalType

import logging

logger = logging.getLogger(__name__)


def signal_type_from_str(s: str) -> SignalType:
    """字符串信号类型 → SignalType 枚举（DB 存字符串，引擎用枚举）。"""
    for st in SignalType:
        if st.value == s:
            return st
    raise ValueError("unknown signal_type: %s" % s)


def assemble_portfolio(ps: PortfolioStrategy, strategies: List[Strategy], db: Session) -> Portfolio:
    """组装 Portfolio + 各 StrategyContext（信号配置 + 策略风控）。

    cost_params 来自组合表，供 SimulatedDispatcher（回测）透传成本参数；
    实盘 HttpBridgeDispatcher 不用 cost_params（真实成本从 /deals 回报取），
    但 Portfolio 仍持有以备恢复/评估时记账。
    """
    pm = PortfolioRiskManager(
        max_drawdown=Decimal(str(ps.max_drawdown)),
        daily_loss_limit=Decimal(str(ps.daily_loss_limit)),
    )
    port = Portfolio(
        portfolio_id=ps.id,
        initial_capital=Decimal(str(ps.initial_capital)),
        risk_manager=pm,
        cost_params={
            "min_commission": Decimal(str(ps.min_commission)),
            "buy_commission_rate": Decimal(str(ps.buy_commission_rate)),
            "sell_commission_rate": Decimal(str(ps.sell_commission_rate)),
            "stamp_duty_rate": Decimal(str(ps.stamp_duty_rate)),
            "slippage": Decimal(str(ps.slippage)),
        },
    )
    # 组合级股票池成分（继承用）：策略 stock_pool_id 为空时回退到此
    portfolio_pool_codes = {
        row.stock_code
        for row in db.query(StockPoolStock).filter_by(pool_id=ps.stock_pool_id).all()
    }
    for strat in strategies:
        # 策略级股票池：strat.stock_pool_id 非空 → 查独立池；空 → 继承组合级
        if strat.stock_pool_id is not None:
            strategy_pool_codes = {
                row.stock_code
                for row in db.query(StockPoolStock).filter_by(pool_id=strat.stock_pool_id).all()
            }
        else:
            strategy_pool_codes = set(portfolio_pool_codes)
        ctx = StrategyContext(
            strategy_id=strat.id,
            period=strat.period,
            capital_ratio=Decimal(str(strat.capital_ratio)),
            max_positions=strat.max_positions,
            single_open_ratio=Decimal(str(strat.single_open_ratio)),
            add_position_threshold=Decimal(str(strat.add_position_threshold)),
            max_add_count=strat.max_add_count,
            add_position_ratio=Decimal(str(strat.add_position_ratio)),
            reduce_position_ratio=Decimal(str(strat.reduce_position_ratio)),
            role=strat.role,
            master_strategy_id=strat.master_strategy_id,
            stock_pool=strategy_pool_codes,
            slave_exit_mode=strat.slave_exit_mode,
        )
        # 从 formula_signals 表读信号配置
        sigs = db.query(FormulaSignal).filter_by(formula_id=strat.formula_id).all()
        ctx.formula_signals = [
            {
                "signal_name": s.signal_name,
                "signal_type": signal_type_from_str(s.signal_type),
                "trigger_value": s.trigger_value,
            }
            for s in sigs
        ]
        ctx.strategy_risk = StrategyRiskManager(
            stop_loss_ratio=Decimal(str(strat.stop_loss_ratio)),
            take_profit_ratio=Decimal(str(strat.take_profit_ratio)),
            trailing_stop_ratio=Decimal(str(strat.trailing_stop_ratio)),
        )
        port.strategies.append(ctx)
    # 防御性告警：历史脏数据下 slave 池 ⊄ master 池 → 越界股 slave 永远开不了仓。
    # 配置接口已 400 拒绝，此处兜底让运行时可见（不 raise，避免中断回测/实盘）。
    for ctx in port.strategies:
        if ctx.role != "slave" or ctx.master_strategy_id is None:
            continue
        master = next(
            (c for c in port.strategies if c.strategy_id == ctx.master_strategy_id), None
        )
        if master is None:
            continue
        extra = (ctx.stock_pool or set()) - (master.stock_pool or set())
        if extra:
            logger.warning(
                "portfolio %s slave %s 股票池越出 master %s：%d 只（样例 %s），"
                "越界股将无法开仓",
                ps.id, ctx.strategy_id, ctx.master_strategy_id, len(extra),
                sorted(extra)[:5],
            )
    return port
