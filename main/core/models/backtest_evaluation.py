from sqlalchemy import Column, Integer, String, ForeignKey, Numeric, DateTime, Index
from sqlalchemy.sql import func

from .base import Base


class BacktestEvaluation(Base):
    __tablename__ = "backtest_evaluations"

    id = Column(Integer, primary_key=True)
    backtest_record_id = Column(Integer, ForeignKey("backtest_records.id", ondelete="CASCADE"), nullable=False)
    target_type = Column(String(10), nullable=False)
    target_id = Column(Integer, nullable=False)
    total_return = Column(Numeric(10, 4), nullable=True)
    annual_return = Column(Numeric(10, 4), nullable=True)
    max_drawdown = Column(Numeric(10, 4), nullable=True)
    volatility = Column(Numeric(10, 4), nullable=True)
    sharpe_ratio = Column(Numeric(10, 4), nullable=True)
    sortino_ratio = Column(Numeric(10, 4), nullable=True)
    calmar_ratio = Column(Numeric(10, 4), nullable=True)
    win_rate = Column(Numeric(10, 4), nullable=True)
    profit_factor = Column(Numeric(10, 4), nullable=True)
    total_trades = Column(Integer, nullable=True)
    benchmark_return = Column(Numeric(10, 4), nullable=True)
    avg_holding_days = Column(Numeric(10, 4), nullable=True)
    var_95 = Column(Numeric(10, 4), nullable=True)
    cvar_95 = Column(Numeric(10, 4), nullable=True)
    avg_recovery_days = Column(Numeric(10, 4), nullable=True)
    max_recovery_days = Column(Integer, nullable=True)
    ulcer_index = Column(Numeric(10, 4), nullable=True)
    return_stability = Column(Numeric(10, 4), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index(
            "ix_backtest_evaluations_rec_target",
            "backtest_record_id",
            "target_type",
            "target_id",
        ),
    )
