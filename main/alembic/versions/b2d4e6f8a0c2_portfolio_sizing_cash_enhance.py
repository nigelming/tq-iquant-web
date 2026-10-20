"""add position_sizing + cash_enhance fields to portfolio_strategies

Revision ID: b2d4e6f8a0c2
Revises: a1c2e3f4b5d6
Create Date: 2026-10-13 15:00:00.000000

W2：仓位模型改为动态净资产默认（position_sizing='dynamic'），新增现金管理
增强预留字段（cash_enhance_*，默认关）。旧组合迁移后沿用 dynamic（新默认口径）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2d4e6f8a0c2'
down_revision: Union[str, Sequence[str], None] = 'a1c2e3f4b5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'portfolio_strategies',
        sa.Column('position_sizing', sa.String(length=10), nullable=False,
                  server_default='dynamic'),
    )
    op.add_column(
        'portfolio_strategies',
        sa.Column('cash_enhance_enabled', sa.Boolean(), nullable=False,
                  server_default='0'),
    )
    op.add_column(
        'portfolio_strategies',
        sa.Column('cash_enhance_equity_threshold', sa.Numeric(15, 2), nullable=True),
    )
    op.add_column(
        'portfolio_strategies',
        sa.Column('cash_enhance_position_ratio_threshold', sa.Numeric(5, 4), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('portfolio_strategies', 'cash_enhance_position_ratio_threshold')
    op.drop_column('portfolio_strategies', 'cash_enhance_equity_threshold')
    op.drop_column('portfolio_strategies', 'cash_enhance_enabled')
    op.drop_column('portfolio_strategies', 'position_sizing')
