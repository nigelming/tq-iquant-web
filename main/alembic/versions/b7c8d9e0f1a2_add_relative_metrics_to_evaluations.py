"""add regime-alpha suite metrics to backtest_evaluations

Revision ID: b7c8d9e0f1a2
Revises: f2a3b4c5d6e7
Create Date: 2026-09-26 14:00:00.000000

regime-alpha 套件阶段一：backtest_evaluations 新增 19 个可空指标列——
基准相对（beta / Jensen α / 年化超额 / 跟踪误差 / IR / 上下行捕获率 / 超额回撤）、
交易磨损仓位（年化换手 / 成本占比 / 仓位利用率 / 空仓占比 / 最大单票 / HHI）、
路径分布（最长连亏 / 最差日周 / 偏度 / 峰度）。
全部可空：老记录在「重新计算评估」后才会填值；持仓类列无 positions_json 恒 NULL。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c8d9e0f1a2'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NEW_COLUMNS = (
    ('beta', sa.Numeric(10, 4)),
    ('jensen_alpha', sa.Numeric(10, 4)),
    ('excess_annual_return', sa.Numeric(10, 4)),
    ('tracking_error', sa.Numeric(10, 4)),
    ('information_ratio', sa.Numeric(10, 4)),
    ('up_capture_ratio', sa.Numeric(10, 4)),
    ('down_capture_ratio', sa.Numeric(10, 4)),
    ('excess_max_drawdown', sa.Numeric(10, 4)),
    ('annual_turnover', sa.Numeric(10, 4)),
    ('total_cost_ratio', sa.Numeric(10, 6)),
    ('avg_position_utilization', sa.Numeric(10, 4)),
    ('empty_days_ratio', sa.Numeric(10, 4)),
    ('max_single_weight', sa.Numeric(10, 4)),
    ('hhi', sa.Numeric(10, 4)),
    ('max_consecutive_loss_days', sa.Integer()),
    ('worst_daily_return', sa.Numeric(10, 4)),
    ('worst_weekly_return', sa.Numeric(10, 4)),
    ('return_skewness', sa.Numeric(10, 4)),
    ('return_kurtosis', sa.Numeric(10, 4)),
)


def _existing_columns() -> set:
    """反射 backtest_evaluations 当前实际列名。

    守卫背景：本迁移早期版本只含 8 个基准相对列且已应用于开发库，后扩展为 19 列。
    alembic 按 revision id 去重不会重放，upgrade/downgrade 因此都按「实际存在」
    过滤：fresh 库行为不变（全加/全删），旧状态库可安全重放收敛。
    """
    insp = sa.inspect(op.get_bind())
    return {c["name"] for c in insp.get_columns("backtest_evaluations")}


def upgrade() -> None:
    """Upgrade schema."""
    existing = _existing_columns()
    with op.batch_alter_table('backtest_evaluations') as batch_op:
        for name, col_type in _NEW_COLUMNS:
            if name not in existing:
                batch_op.add_column(sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    existing = _existing_columns()
    with op.batch_alter_table('backtest_evaluations') as batch_op:
        for name, _ in reversed(_NEW_COLUMNS):
            if name in existing:
                batch_op.drop_column(name)
