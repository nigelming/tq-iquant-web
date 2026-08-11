from sqlalchemy import Column, Integer, String, ForeignKey, Numeric, DateTime
from sqlalchemy.sql import func

from .base import Base


class Strategy(Base):
    __tablename__ = "strategies"

    id = Column(Integer, primary_key=True)
    portfolio_id = Column(Integer, ForeignKey("portfolio_strategies.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(100), nullable=False)
    formula_id = Column(Integer, ForeignKey("formulas.id", ondelete="RESTRICT"), nullable=False, index=True)
    period = Column(String(5), nullable=False)
    role = Column(String(15), nullable=False)
    # 自引用：slave 指向同组合 master。ondelete=RESTRICT（审计 #18）——业务规则禁止
    # "slave 无 master"的孤儿行，delete_strategy 已有 app 层预检，DB 层 RESTRICT 兜底。
    master_strategy_id = Column(Integer, ForeignKey("strategies.id", ondelete="RESTRICT"), nullable=True, index=True)
    capital_ratio = Column(Numeric(5, 4), default=0.6000, server_default="0.6000", nullable=False)
    max_positions = Column(Integer, default=5, server_default="5", nullable=False)
    single_open_ratio = Column(Numeric(5, 4), default=0.1000, server_default="0.1000", nullable=False)
    stop_loss_ratio = Column(Numeric(5, 4), default=0.0500, server_default="0.0500", nullable=False)
    take_profit_ratio = Column(Numeric(5, 4), default=0.1500, server_default="0.1500", nullable=False)
    trailing_stop_ratio = Column(Numeric(5, 4), default=0.0300, server_default="0.0300", nullable=False)
    add_position_threshold = Column(Numeric(5, 4), default=0.0500, server_default="0.0500", nullable=False)
    max_add_count = Column(Integer, default=2, server_default="2", nullable=False)
    add_position_ratio = Column(Numeric(5, 4), default=0.1000, server_default="0.1000", nullable=False)
    reduce_position_ratio = Column(Numeric(5, 4), default=0.3000, server_default="0.3000", nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
