"""add portfolio_strategies.equity_cap (capacity ceiling for dynamic sizing)

Revision ID: e3e2d1c0b9a8
Revises: f9a8b7c6d5e4
Create Date: 2026-10-14 09:00:00.000000

W3 容量上限：dynamic 仓位下资金 = capital_ratio × min(当前净值, equity_cap)，
净值超上限后仓位封顶、超额现金闲置，封堵长周期复利下不可实现的天文仓位。
None = 不设上限（历史行为不变）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e3e2d1c0b9a8'
down_revision: Union[str, Sequence[str], None] = 'f9a8b7c6d5e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'portfolio_strategies',
        sa.Column('equity_cap', sa.Numeric(15, 2), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('portfolio_strategies', 'equity_cap')
