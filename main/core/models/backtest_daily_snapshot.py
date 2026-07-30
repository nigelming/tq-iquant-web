from sqlalchemy import Column, Integer, String, ForeignKey, Date, Numeric, DateTime, Text, Index
from sqlalchemy.sql import func

from .base import Base


class BacktestDailySnapshot(Base):
    __tablename__ = "backtest_daily_snapshots"

    id = Column(Integer, primary_key=True)
    backtest_record_id = Column(Integer, ForeignKey("backtest_records.id", ondelete="CASCADE"), nullable=False)
    target_type = Column(String(10), nullable=False)
    target_id = Column(Integer, nullable=False)
    snap_date = Column(Date, nullable=False)
    total_value = Column(Numeric(15, 2), nullable=False)
    cash = Column(Numeric(15, 2), nullable=False)
    market_value = Column(Numeric(15, 2), nullable=False)
    daily_return = Column(Numeric(10, 6), nullable=True)
    cumulative_return = Column(Numeric(10, 6), nullable=True)
    benchmark_value = Column(Numeric(15, 2), nullable=True)
    positions_json = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index(
            "ix_backtest_snapshots_rec_target_date",
            "backtest_record_id",
            "target_type",
            "target_id",
            "snap_date",
        ),
    )
