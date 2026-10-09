from sqlalchemy import Column, Integer, String, Date, DateTime, Text, ForeignKey
from sqlalchemy.sql import func

from .base import Base


class BacktestRecord(Base):
    __tablename__ = "backtest_records"

    id = Column(Integer, primary_key=True)
    portfolio_strategy_id = Column(Integer, ForeignKey("portfolio_strategies.id", ondelete="RESTRICT"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    status = Column(String(10), nullable=False)
    error_message = Column(Text, nullable=True)
    params_snapshot = Column(Text, nullable=True)
    # 信号预检警告（JSON list[str]）：公式报错/输出线缺失/区间内零信号等
    # 非致命问题——回测仍 completed，但前端详情页置顶提示"为什么没有交易"。
    warnings_json = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)
