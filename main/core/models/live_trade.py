from sqlalchemy import Column, Integer, String, ForeignKey, Numeric, DateTime, Index
from sqlalchemy.sql import func

from .base import Base


class LiveTrade(Base):
    __tablename__ = "live_trades"

    id = Column(Integer, primary_key=True)
    live_session_id = Column(Integer, ForeignKey("live_sessions.id", ondelete="CASCADE"), nullable=False)
    live_order_id = Column(Integer, ForeignKey("live_orders.id"), nullable=True)
    portfolio_strategy_id = Column(Integer, ForeignKey("portfolio_strategies.id"), nullable=False)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False)
    stock_code = Column(String(20), nullable=False)
    trade_type = Column(String(4), nullable=False)
    price = Column(Numeric(10, 3), nullable=False)
    quantity = Column(Integer, nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)
    commission = Column(Numeric(10, 2), nullable=False)
    stamp_duty = Column(Numeric(10, 2), nullable=False)
    trade_time = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index("ix_live_trades_session_time", "live_session_id", "trade_time"),
        Index("ix_live_trades_order", "live_order_id"),
        Index("ix_live_trades_portfolio_time", "portfolio_strategy_id", "trade_time"),
    )
