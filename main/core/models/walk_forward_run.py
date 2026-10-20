from sqlalchemy import Column, Integer, String, Date, DateTime, Text, ForeignKey, Numeric
from sqlalchemy.sql import func

from .base import Base


class WalkForwardRun(Base):
    """C8 walk-forward 滚动验证：滚动 train_years 年训练 + test_years 年验证逐段前移。

    一次 run 生成多个窗口子回测（backtest_records.walk_forward_run_id 关联），
    每窗按 oos_ratio=test/(train+test) 切 IS/OOS 并过 C7 门槛；
    summary_json 落各窗明细与 overall_pass（全窗 passed 才 True）。
    """
    __tablename__ = "walk_forward_runs"

    id = Column(Integer, primary_key=True)
    portfolio_strategy_id = Column(Integer, ForeignKey("portfolio_strategies.id", ondelete="RESTRICT"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    start_date = Column(Date, nullable=False)
    end_date = Column(Date, nullable=False)
    train_years = Column(Integer, nullable=False, default=3, server_default="3")
    test_years = Column(Integer, nullable=False, default=1, server_default="1")
    oos_gate_min_ratio = Column(Numeric(4, 2), nullable=False, default=0.5, server_default="0.50")
    status = Column(String(10), nullable=False)  # running/completed/failed
    summary_json = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)
