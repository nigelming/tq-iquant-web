"""add closed_trades to backtest_evaluations

Revision ID: a1c2e3f4b5d6
Revises: e7f8a9b0c1d2
Create Date: 2026-10-13 14:00:00.000000

W1.1/W1.2：评估新增 closed_trades（FIFO 平仓批次数），与 total_trades
（成交笔数）口径区分。win_rate/profit_factor 基于平仓批次。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c2e3f4b5d6'
down_revision: Union[str, Sequence[str], None] = 'e7f8a9b0c1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'backtest_evaluations',
        sa.Column('closed_trades', sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('backtest_evaluations', 'closed_trades')
