from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.sql import func

from .base import Base


class BacktestCrisisAlpha(Base):
    __tablename__ = "backtest_crisis_alphas"

    id = Column(Integer, primary_key=True)
    backtest_record_id = Column(
        Integer,
        ForeignKey("backtest_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    benchmark_code = Column(String(20), nullable=False)
    benchmark_name = Column(String(20), nullable=False)
    status = Column(String(12), nullable=False)
    error = Column(Text, nullable=True)
    skipped_benchmarks_json = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=True)
    computed_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("backtest_record_id", name="uq_backtest_crisis_alphas_record"),
    )
