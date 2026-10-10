from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.sql import func

from .base import Base


class BacktestMarketRegimeAnalysis(Base):
    """市场状态×绩效分析（机遇/中性Alpha 专题，FR-11）。

    与 ``backtest_crisis_alphas`` 同模式：每回测记录一行（唯一约束），
    payload_json 存 RegimeAlphaAnalyzer 完整结果；分析失败写 status=failed
    与 error，不影响回测详情主链路。skipped_benchmarks_json 预留（阶段三
    多基准风格矩阵沿用）。
    """
    __tablename__ = "backtest_market_regime_analyses"

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
        UniqueConstraint(
            "backtest_record_id", name="uq_backtest_market_regime_record"),
    )
