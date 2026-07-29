from sqlalchemy import Column, Integer, String, ForeignKey, UniqueConstraint

from .base import Base


class LiveSessionPortfolio(Base):
    __tablename__ = "live_session_portfolios"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("live_sessions.id", ondelete="CASCADE"), nullable=False)
    portfolio_strategy_id = Column(Integer, ForeignKey("portfolio_strategies.id"), nullable=False)
    status = Column(String(15), default="active")

    __table_args__ = (UniqueConstraint("session_id", "portfolio_strategy_id"),)
