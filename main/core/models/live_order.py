from sqlalchemy import Column, Integer, String, ForeignKey, Numeric, DateTime, Index
from sqlalchemy.sql import func

from .base import Base


class LiveOrder(Base):
    __tablename__ = "live_orders"

    id = Column(Integer, primary_key=True)
    live_session_id = Column(Integer, ForeignKey("live_sessions.id", ondelete="CASCADE"), nullable=False)
    portfolio_strategy_id = Column(Integer, ForeignKey("portfolio_strategies.id"), nullable=False)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False)
    stock_code = Column(String(20), nullable=False)
    trade_type = Column(String(4), nullable=False)
    order_type = Column(String(10), nullable=False)
    price = Column(Numeric(10, 3), nullable=True)
    quantity = Column(Integer, nullable=False)
    filled_quantity = Column(Integer, default=0)
    filled_price = Column(Numeric(10, 3), nullable=True)
    status = Column(String(15), nullable=False)
    error_message = Column(String(500), nullable=True)
    nats_request_id = Column(String(64), nullable=True)
    signal_name = Column(String(50), nullable=True)
    signal_type = Column(String(15), nullable=True)
    bar_time = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index("ix_live_orders_session_status", "live_session_id", "status"),
        Index("ix_live_orders_portfolio", "portfolio_strategy_id"),
    )
