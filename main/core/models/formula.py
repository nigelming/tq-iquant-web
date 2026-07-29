from sqlalchemy import Column, Integer, String, Text, DateTime
from sqlalchemy.sql import func

from .base import Base


class Formula(Base):
    __tablename__ = "formulas"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
