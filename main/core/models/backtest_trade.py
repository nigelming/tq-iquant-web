from sqlalchemy import Column, Integer, String, ForeignKey, Numeric, DateTime
from sqlalchemy.sql import func

from .base import Base


class BacktestTrade(Base):
    __tablename__ = "backtest_trades"

    id = Column(Integer, primary_key=True)
    backtest_record_id = Column(Integer, ForeignKey("backtest_records.id"), nullable=False)
    strategy_id = Column(Integer, ForeignKey("strategies.id"), nullable=False)
    formula_signal_id = Column(Integer, ForeignKey("formula_signals.id"), nullable=True)
    signal_name = Column(String(50), nullable=False)
    signal_type = Column(String(15), nullable=False)
    stock_code = Column(String(20), nullable=False)
    trade_type = Column(String(4), nullable=False)
    price = Column(Numeric(10, 3), nullable=False)
    quantity = Column(Integer, nullable=False)
    amount = Column(Numeric(15, 2), nullable=False)
    commission = Column(Numeric(10, 2), nullable=False)
    stamp_duty = Column(Numeric(10, 2), nullable=False)
    bar_time = Column(DateTime, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
