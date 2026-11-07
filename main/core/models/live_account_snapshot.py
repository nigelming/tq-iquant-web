from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, Integer, Numeric, String,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from .base import Base


class LiveAccountSnapshot(Base):
    """实盘账户快照（账户全貌对齐）。

    周期采集交易引擎真实账户状态（总资产/可用资金/市值 + 持仓明细），
    用于：
    - BUY 资金审批对齐（min(虚拟现金, 真实可用×0.98)）；
    - 外部持仓（柜台原有、非引擎买入）识别与导入虚拟账本（纳入风控）；
    - 回测继承与实盘账户全貌比对（工作台展示）。

    采集节拍按通道：tdx/iquant 5s（deals 节拍）、ths 60s（UI 读表重）。
    保留策略：采集方负责清理 7 天前的历史行。
    """
    __tablename__ = "live_account_snapshots"

    id = Column(Integer, primary_key=True)
    live_session_id = Column(
        Integer, ForeignKey("live_sessions.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    engine = Column(String(15), nullable=False)
    captured_at = Column(DateTime, nullable=False, index=True)
    total_asset = Column(Numeric(16, 2), nullable=True)
    available_cash = Column(Numeric(16, 2), nullable=True)
    market_value = Column(Numeric(16, 2), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    positions = relationship(
        "LiveAccountPositionSnapshot",
        cascade="all, delete-orphan",
        back_populates="snapshot",
        lazy="joined",
    )

    __table_args__ = (
        Index("ix_live_account_snapshots_session_time",
              "live_session_id", "captured_at"),
    )


class LiveAccountPositionSnapshot(Base):
    """实盘账户快照持仓明细（一行 = 真实账户一只持仓）。"""
    __tablename__ = "live_account_position_snapshots"

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(
        Integer,
        ForeignKey("live_account_snapshots.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    stock_code = Column(String(20), nullable=False, index=True)
    quantity = Column(Integer, nullable=False, default=0)
    available_quantity = Column(Integer, nullable=True)
    cost_price = Column(Numeric(10, 4), nullable=True)
    current_price = Column(Numeric(10, 4), nullable=True)
    market_value = Column(Numeric(16, 2), nullable=True)
    # 移动止损峰值持久化：重启后导入/恢复持仓时还原 highest_price
    # （否则基准从重启时价格重新起算，本该触发的移动止损会延迟）
    highest_price = Column(Numeric(10, 4), nullable=True)

    snapshot = relationship(
        "LiveAccountSnapshot", back_populates="positions",
    )
