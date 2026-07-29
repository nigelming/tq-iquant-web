from sqlalchemy import Column, Integer, String, ForeignKey, Numeric, DateTime
from sqlalchemy.sql import func

from .base import Base


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True)
    portfolio_id = Column(Integer, ForeignKey("portfolio_strategies.id"), nullable=False)
    name = Column(String(100), nullable=False)
    formula_id = Column(Integer, ForeignKey("formulas.id"), nullable=False)
    period = Column(String(5), nullable=False)
    role = Column(String(15), nullable=False)
    master_strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=True)
    capital_ratio = Column(Numeric(5, 4), default=0.6000)
    max_positions = Column(Integer, default=5)
    single_open_ratio = Column(Numeric(5, 4), default=0.1000)
    stop_loss_ratio = Column(Numeric(5, 4), default=0.0500)
    take_profit_ratio = Column(Numeric(5, 4), default=0.1500)
    trailing_stop_ratio = Column(Numeric(5, 4), default=0.0300)
    add_position_threshold = Column(Numeric(5, 4), default=0.0500)
    max_add_count = Column(Integer, default=2)
    add_position_ratio = Column(Numeric(5, 4), default=0.1000)
    reduce_position_ratio = Column(Numeric(5, 4), default=0.3000)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
