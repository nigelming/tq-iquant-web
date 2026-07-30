from sqlalchemy import Column, Integer, String, DateTime, Numeric, ForeignKey
from sqlalchemy.sql import func

from .base import Base


class PortfolioStrategy(Base):
    __tablename__ = "portfolio_strategies"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    stock_pool_id = Column(Integer, ForeignKey("stock_pools.id", ondelete="RESTRICT"), nullable=False)
    benchmark_index = Column(String(20), default="000300.SH")
    initial_capital = Column(Numeric(15, 2), default=500000)
    max_drawdown = Column(Numeric(5, 4), default=0.2000)
    daily_loss_limit = Column(Numeric(5, 4), default=0.0500)
    max_holdings = Column(Integer, default=10)
    min_commission = Column(Numeric(10, 2), default=5)
    buy_commission_rate = Column(Numeric(8, 6), default=0.000250)
    sell_commission_rate = Column(Numeric(8, 6), default=0.000250)
    stamp_duty_rate = Column(Numeric(8, 6), default=0.000500)
    slippage = Column(Numeric(8, 6), default=0)
    trading_session = Column(String(10), default="full")
    status = Column(String(10), default="active")
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
