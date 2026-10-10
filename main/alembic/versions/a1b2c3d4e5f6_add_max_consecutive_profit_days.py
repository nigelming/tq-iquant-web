"""add max_consecutive_profit_days to backtest_evaluations

Revision ID: b2c3d4e5f6a7
Revises: f3417686e6fb
Create Date: 2026-09-29 12:00:00.000000

路径质量第 6 指标：最长连盈天数，与最长连亏天数对称。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'backtest_evaluations',
        sa.Column('max_consecutive_profit_days', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('backtest_evaluations', 'max_consecutive_profit_days')
