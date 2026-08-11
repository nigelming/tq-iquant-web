from sqlalchemy import Column, Integer, String, DateTime, Numeric, ForeignKey
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
    status = Column(String(10), default="active", server_default="active", nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
