from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, Integer, Numeric, String,
    UniqueConstraint,
)
from sqlalchemy.sql import func

from .base import Base


class LiveSignal(Base):
    __tablename__ = "live_signals"

    id = Column(Integer, primary_key=True)
    live_session_id = Column(
        Integer, ForeignKey("live_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    portfolio_strategy_id = Column(
        Integer, ForeignKey("portfolio_strategies.id"), nullable=False, index=True,
    )
    strategy_id = Column(
        Integer, ForeignKey("strategies.id"), nullable=False, index=True,
    )
    formula_id = Column(Integer, ForeignKey("formulas.id"), nullable=True)
    stock_code = Column(String(20), nullable=False)
    period = Column(String(5), nullable=False)
    bar_time = Column(DateTime, nullable=False)
    formula_name = Column(String(100), nullable=False)
    signal_name = Column(String(50), nullable=False)
    signal_type = Column(String(10), nullable=False)
    trigger_value = Column(Integer, nullable=False)
    price = Column(Numeric(10, 3), nullable=True)
    execution_mode = Column(String(10), nullable=False)
    status = Column(String(15), nullable=False, server_default="new", default="new")
    alert_status = Column(String(15), nullable=True)
    alert_error = Column(String(500), nullable=True)
    tdx_run_id = Column(String(30), nullable=True)
    alerted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "live_session_id", "portfolio_strategy_id", "strategy_id",
            "stock_code", "signal_name", "bar_time",
            name="uq_live_signal_dedup",
        ),
        Index("ix_live_signals_session_bar", "live_session_id", "bar_time"),
        Index("ix_live_signals_strategy_stock", "strategy_id", "stock_code"),
    )
