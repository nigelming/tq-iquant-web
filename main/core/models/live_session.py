from sqlalchemy import Column, Integer, String, DateTime, Numeric
from sqlalchemy.sql import func

from .base import Base


class LiveSession(Base):
    __tablename__ = "live_sessions"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False)
    mode = Column(String(10), nullable=False)
    # 交易引擎：iquant(QMT 通道) / tdx(通达信TQ) / ths(同花顺UI)。
    # 为 NULL 表示迁移前的历史会话，运行时按全局默认引擎（ths_ui.enabled 判定）解析，
    # 保持升级前行为不变；新建会话一律显式写入。
    # 历史 qmt 会话（通道已移除）由 session_engine 白名单回退到默认引擎。
    engine = Column(String(10), nullable=True)
    # 实盘资金基准（账户总资产口径，元）：None=各组合用自身 initial_capital；
    # 设置=会话内所有组合以该值起账（资金占比/单仓/敞口等比率配置等比缩放），
    # 多组合各自独立记账。启动后继承持仓的市值会从起账现金中扣除（避免与基准双计）。
    live_capital = Column(Numeric(18, 2), nullable=True)
    # 执行模式：alert（半自动，只预警不下单）/ auto（现有自动交易）。
    # 迁移前历史会话写 auto；新建会话由 live_service 显式写 alert。
    execution_mode = Column(String(10), nullable=False, server_default="auto")
    # 外部信号注入（0011）：同花顺条件预警输出文件路径。NULL=不启用（行为不变）；
    # 配置后引擎每轮增量读取该文件，公式名匹配策略的信号走全套注入链。
    signal_file_path = Column(String(500), nullable=True)
    status = Column(String(10), nullable=False)
    created_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
    started_at = Column(DateTime, nullable=True)
    stopped_at = Column(DateTime, nullable=True)
