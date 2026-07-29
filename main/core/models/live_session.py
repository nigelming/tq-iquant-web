from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.sql import func

from .base import Base


class LiveSession(Base):
    __tablename__ = "live_sessions"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    mode = Column(String(10), nullable=False)
    status = Column(String(10), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    started_at = Column(DateTime, nullable=True)
    stopped_at = Column(DateTime, nullable=True)
