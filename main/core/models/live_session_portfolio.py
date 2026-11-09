from sqlalchemy import Column, Integer, String, ForeignKey, UniqueConstraint, DateTime, Text
from sqlalchemy.sql import func

from .base import Base


class LiveSessionPortfolio(Base):
    __tablename__ = "live_session_portfolios"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("live_sessions.id", ondelete="CASCADE"), nullable=False)
    portfolio_strategy_id = Column(Integer, ForeignKey("portfolio_strategies.id", ondelete="RESTRICT"), nullable=False)
    status = Column(String(15), default="active", server_default="active", nullable=False)
    circuit_breaker_count = Column(Integer, default=0, server_default="0", nullable=False)
    # 风控内存态快照（JSON 文本）：peak_value/prev_close_value/daily_pause_active/
    # breaker_trigger_date/daily_loss_trigger_date。实盘重启时读回，避免峰值/暂停态丢失
    # 导致 max_drawdown 少触发、日内暂停被意外解除。日终与触发时落库。
    risk_state = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())

    __table_args__ = (UniqueConstraint("session_id", "portfolio_strategy_id"),)
