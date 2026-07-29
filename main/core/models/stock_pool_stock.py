from sqlalchemy import Column, Integer, String, ForeignKey, UniqueConstraint

from .base import Base


class StockPoolStock(Base):
    __tablename__ = "stock_pool_stocks"

    id = Column(Integer, primary_key=True)
    pool_id = Column(Integer, ForeignKey("stock_pools.id"), nullable=False)
    stock_code = Column(String(20), nullable=False)
    stock_name = Column(String(50), nullable=True)

    __table_args__ = (UniqueConstraint("pool_id", "stock_code"),)
