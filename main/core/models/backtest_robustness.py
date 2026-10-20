from sqlalchemy import Column, Integer, String, Text, ForeignKey, DateTime, UniqueConstraint
from sqlalchemy.sql import func

from .base import Base


class BacktestRobustnessAnalysis(Base):
    """回测稳健性检验（年度/3年滚动/个股集中度/样本内外对比）。

    与 ``backtest_market_regime_analyses`` 同模式：每回测记录一行（唯一约束），
    payload_json 存 analyze_robustness 完整结果；分析失败写 status=failed 与 error，
    不影响回测详情主链路。纯后处理（只读已落库快照+成交），不重跑回测，
    因此与复权模式（front/back/dual）无关、天然兼容。
    """
    __tablename__ = "backtest_robustness_analyses"

    id = Column(Integer, primary_key=True)
    backtest_record_id = Column(
        Integer,
        ForeignKey("backtest_records.id", ondelete="CASCADE"),
        nullable=False,
    )
    status = Column(String(12), nullable=False)
    error = Column(Text, nullable=True)
    payload_json = Column(Text, nullable=True)
    computed_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint(
            "backtest_record_id", name="uq_backtest_robustness_record"),
    )
