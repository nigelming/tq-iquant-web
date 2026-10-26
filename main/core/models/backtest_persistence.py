from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.sql import func

from .base import Base


class BacktestPersistenceAnalysis(Base):
    """持续性检验（分年度超额/风险/交易结构 + 滚动年化，判定阿尔法衰减 vs 风格钟摆）。

    与 ``backtest_crisis_alphas`` 同模式：每回测一行，唯一约束。payload_json 为
    persistence_alpha.compute_persistence 结果；失败写 status=failed + error。
    """
    __tablename__ = "backtest_persistence_analyses"

    id = Column(Integer, primary_key=True)
    backtest_record_id = Column(
        Integer,
        ForeignKey("backtest_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    status = Column(String(12), nullable=False)
    error = Column(Text, nullable=True)
    skipped_benchmarks_json = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=True)
    computed_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("backtest_record_id", name="uq_backtest_persistence_record"),
    )
