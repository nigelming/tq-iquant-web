from sqlalchemy import Boolean, Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func

from .base import Base


class Formula(Base):
    __tablename__ = "formulas"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    # Q4 决策4：公式注入历史根数（公式固有属性，人工按公式内容填最小 bar 数，默认 200）
    formula_count = Column(Integer, nullable=False, server_default="200", default=200)
    # 计算引擎：tq=按名调通达信（默认，历史行为）/ builtin=内置公式子集解释器
    # （对回测喂入的 klines 逐 bar 求值，仅回测接线，实盘仍走 tq）
    engine = Column(String(10), nullable=False, server_default="tq", default="tq")
    # 批次 9a：跨周期引用（C#WEEK）无泄露开关，默认 false=TDX 语义（所属上级 bar
    # 未完成时引用其整根终值，周内含未来引用）；true=仅引用已完成上级 bar。
    # 仅影响 engine=builtin 回测链路，实盘/TQ 路径不受影响
    no_leak_cross = Column(Boolean, nullable=False, server_default="0", default=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
