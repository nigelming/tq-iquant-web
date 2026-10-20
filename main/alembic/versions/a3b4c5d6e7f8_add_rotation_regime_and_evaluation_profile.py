"""add rotation/regime columns to strategies, evaluation_profile to backtest_records

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e8
Create Date: 2026-10-13 10:00:00.000000

B 层结构优化（全部默认关，关闭时行为与原逻辑一致）：
- B4 OPEN 择优轮动：strategies 增 rotation_enabled / rotation_momentum_bars /
  rotation_hysteresis（满槽时强信号轮出最弱持仓换入新信号股）。
- B5 市场状态闸：strategies 增 regime_filter_enabled / trend_trailing_stop_ratio /
  trend_max_positions（基准周线 bull 态用 trend_* 覆盖参数）。
- B6 考核口径：backtest_records 增 evaluation_profile（absolute 默认 / relative）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, Sequence[str], None] = 'f2a3b4c5d6e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'strategies',
        sa.Column('rotation_enabled', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    )
    op.add_column(
        'strategies',
        sa.Column('rotation_momentum_bars', sa.Integer(), server_default='20', nullable=False),
    )
    op.add_column(
        'strategies',
        sa.Column('rotation_hysteresis', sa.Numeric(5, 4), server_default='0.0500', nullable=False),
    )
    op.add_column(
        'strategies',
        sa.Column('regime_filter_enabled', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    )
    op.add_column(
        'strategies',
        sa.Column('trend_trailing_stop_ratio', sa.Numeric(5, 4), nullable=True),
    )
    op.add_column(
        'strategies',
        sa.Column('trend_max_positions', sa.Integer(), nullable=True),
    )
    op.add_column(
        'backtest_records',
        sa.Column('evaluation_profile', sa.String(length=10), server_default='absolute', nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('backtest_records', 'evaluation_profile')
    op.drop_column('strategies', 'trend_max_positions')
    op.drop_column('strategies', 'trend_trailing_stop_ratio')
    op.drop_column('strategies', 'regime_filter_enabled')
    op.drop_column('strategies', 'rotation_hysteresis')
    op.drop_column('strategies', 'rotation_momentum_bars')
    op.drop_column('strategies', 'rotation_enabled')
