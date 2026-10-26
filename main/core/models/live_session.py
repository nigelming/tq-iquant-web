from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func

from .base import Base


class LiveSession(Base):
    __tablename__ = "live_sessions"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    mode = Column(String(10), nullable=False)
    # 执行模式：alert（半自动，只预警不下单）/ auto（现有自动交易）。
    # 迁移前历史会话写 auto；新建会话由 live_service 显式写 alert。
    execution_mode = Column(String(10), nullable=False, server_default="auto")
    status = Column(String(10), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    started_at = Column(DateTime, nullable=True)
    stopped_at = Column(DateTime, nullable=True)
