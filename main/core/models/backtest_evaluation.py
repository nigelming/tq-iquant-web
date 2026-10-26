from sqlalchemy import Column, Integer, String, ForeignKey, Numeric, DateTime, Index
from sqlalchemy.sql import func

from .base import Base


class BacktestEvaluation(Base):
    __tablename__ = "backtest_evaluations"

    id = Column(Integer, primary_key=True)
    backtest_record_id = Column(Integer, ForeignKey("backtest_records.id", ondelete="CASCADE"), nullable=False)
    target_type = Column(String(10), nullable=False)
    target_id = Column(Integer, nullable=False)
    total_return = Column(Numeric(10, 4), nullable=True)
    annual_return = Column(Numeric(10, 4), nullable=True)
    max_drawdown = Column(Numeric(10, 4), nullable=True)
    volatility = Column(Numeric(10, 4), nullable=True)
    sharpe_ratio = Column(Numeric(10, 4), nullable=True)
    sortino_ratio = Column(Numeric(10, 4), nullable=True)
    calmar_ratio = Column(Numeric(10, 4), nullable=True)
    win_rate = Column(Numeric(10, 4), nullable=True)
    profit_factor = Column(Numeric(10, 4), nullable=True)
    total_trades = Column(Integer, nullable=True)
    # FIFO 平仓批次数（与 total_trades 成交笔数口径区分：win_rate/profit_factor 按平仓批次）
    closed_trades = Column(Integer, nullable=True)
    benchmark_return = Column(Numeric(10, 4), nullable=True)
    avg_holding_days = Column(Numeric(10, 4), nullable=True)
    var_95 = Column(Numeric(10, 4), nullable=True)
    cvar_95 = Column(Numeric(10, 4), nullable=True)
    avg_recovery_days = Column(Numeric(10, 4), nullable=True)
    max_recovery_days = Column(Integer, nullable=True)
    ulcer_index = Column(Numeric(10, 4), nullable=True)
    return_stability = Column(Numeric(10, 4), nullable=True)
    # ---- 基准相对指标（2026-09 regime-alpha 套件，仅组合级评估可能出数）----
    beta = Column(Numeric(10, 4), nullable=True)
    jensen_alpha = Column(Numeric(10, 4), nullable=True)
    excess_annual_return = Column(Numeric(10, 4), nullable=True)
    tracking_error = Column(Numeric(10, 4), nullable=True)
    information_ratio = Column(Numeric(10, 4), nullable=True)
    up_capture_ratio = Column(Numeric(10, 4), nullable=True)
    down_capture_ratio = Column(Numeric(10, 4), nullable=True)
    excess_max_drawdown = Column(Numeric(10, 4), nullable=True)
    # ---- 交易磨损 / 仓位 / 结构（FR-2）----
    annual_turnover = Column(Numeric(10, 4), nullable=True)
    total_cost_ratio = Column(Numeric(10, 6), nullable=True)
    avg_position_utilization = Column(Numeric(10, 4), nullable=True)
    empty_days_ratio = Column(Numeric(10, 4), nullable=True)
    max_single_weight = Column(Numeric(10, 4), nullable=True)
    hhi = Column(Numeric(10, 4), nullable=True)
    # ---- 路径 / 分布（FR-3）----
    max_consecutive_loss_days = Column(Integer, nullable=True)
    max_consecutive_profit_days = Column(Integer, nullable=True)
    worst_daily_return = Column(Numeric(10, 4), nullable=True)
    worst_weekly_return = Column(Numeric(10, 4), nullable=True)
    return_skewness = Column(Numeric(10, 4), nullable=True)
    return_kurtosis = Column(Numeric(10, 4), nullable=True)
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        Index(
            "ix_backtest_evaluations_rec_target",
            "backtest_record_id",
            "target_type",
            "target_id",
        ),
    )
