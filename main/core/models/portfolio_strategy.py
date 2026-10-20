from sqlalchemy import Column, Integer, String, DateTime, Numeric, ForeignKey, Boolean
from sqlalchemy.sql import func

from .base import Base


class PortfolioStrategy(Base):
    __tablename__ = "portfolio_strategies"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    stock_pool_id = Column(Integer, ForeignKey("stock_pools.id", ondelete="RESTRICT"), nullable=False, index=True)
    benchmark_index = Column(String(20), default="000300.SH", server_default="000300.SH", nullable=False)
    initial_capital = Column(Numeric(15, 2), default=500000, server_default="500000", nullable=False)
    max_drawdown = Column(Numeric(5, 4), default=0.2000, server_default="0.2000", nullable=False)
    daily_loss_limit = Column(Numeric(5, 4), default=0.0500, server_default="0.0500", nullable=False)
    max_holdings = Column(Integer, default=10, server_default="10", nullable=False)
    min_commission = Column(Numeric(10, 2), default=5, server_default="5", nullable=False)
    buy_commission_rate = Column(Numeric(8, 6), default=0.000250, server_default="0.000250", nullable=False)
    sell_commission_rate = Column(Numeric(8, 6), default=0.000250, server_default="0.000250", nullable=False)
    stamp_duty_rate = Column(Numeric(8, 6), default=0.000500, server_default="0.000500", nullable=False)
    slippage = Column(Numeric(8, 6), default=0, server_default="0", nullable=False)
    trading_session = Column(String(10), default="full", server_default="full", nullable=False)
    # #1 涨跌停约束（回测成交步）：True=涨停买不进/跌停卖不出（延后重试）。
    # 默认开（含 ORM/server 默认）——无约束回测会系统性高估可成交性；旧组合由迁移置 1。
    price_limit_check_enabled = Column(Boolean, default=True, server_default="1", nullable=False)
    # #2 成交量/流动性约束（回测成交步，可选默认关）：单 bar 成交量 × ratio 封顶成交股数。
    volume_limit_enabled = Column(Boolean, default=False, server_default="0", nullable=False)
    volume_limit_ratio = Column(Numeric(4, 2), default=0.10, server_default="0.10", nullable=False)
    # #4 自适应滑点（回测成交步，可选默认关）：开启后有效滑点 =
    # slippage + slippage_impact_coef × 订单股数/(bar成交量×100)，封顶 0.10，
    # 模拟市场冲击成本（订单量占当日成交量比例越大滑点越高）；与 volume_limit
    # 天然协同（先缩量后按缩量后 quantity 算冲击）。默认关行为与原逻辑一致。
    adaptive_slippage_enabled = Column(Boolean, default=False, server_default="0", nullable=False)
    slippage_impact_coef = Column(Numeric(6, 4), default=0.10, server_default="0.1000", nullable=False)
    # W2 仓位模型：dynamic=按组合当前总净值分配策略资金（默认，随盈亏伸缩）；
    # fixed=按 initial_capital 固定名义（旧行为，风险调整指标被系统性低估）。
    position_sizing = Column(String(10), default="dynamic", server_default="dynamic", nullable=False)
    # W3 容量上限（可选，默认 None）：dynamic 下资金 = capital_ratio × min(当前净值, 上限)。
    # 净值超上限后仓位封顶、超额现金闲置（等价"超容量部分撤回/封盘"）——封堵
    # "20%×不断增长的净值"在长周期复利下开出不可实现的天文仓位（真实容量天花板）。
    # fixed 模式不受影响（本就按 initial_capital 固定）。
    equity_cap = Column(Numeric(15, 2), nullable=True)
    # W2 现金管理增强（预留触发点，默认关）：命中阈值仅记决策事件，不自动下单。
    cash_enhance_enabled = Column(Boolean, default=False, server_default="0", nullable=False)
    cash_enhance_equity_threshold = Column(Numeric(15, 2), nullable=True)
    cash_enhance_position_ratio_threshold = Column(Numeric(5, 4), nullable=True)
    status = Column(String(10), default="active", server_default="active", nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
