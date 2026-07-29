from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func

from .base import Base


class StockPool(Base):
    __tablename__ = "stock_pools"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    synced_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now())
