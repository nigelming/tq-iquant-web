from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func

from .base import Base


class StockPool(Base):
    __tablename__ = "stock_pools"

    id = Column(Integer, primary_key=True)
    code = Column(String(50), nullable=False, unique=True)  # 通达信板块 Code（如 TQCS），同步成分股的依据；unique 防重复同步（审计 #19）
    name = Column(String(100), nullable=False)
    synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
