from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func

from .base import Base


class Formula(Base):
    __tablename__ = "formulas"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    # Q4 决策4：公式注入历史根数（公式固有属性，人工按公式内容填最小 bar 数，默认 200）
    formula_count = Column(Integer, nullable=False, server_default="200", default=200)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
