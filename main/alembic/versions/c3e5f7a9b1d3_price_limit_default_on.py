"""price_limit_check_enabled default on

Revision ID: c3e5f7a9b1d3
Revises: b2d4e6f8a0c2
Create Date: 2026-10-13 16:00:00.000000

W3：涨跌停约束默认开启（server_default '1'），并把既有组合一次性置 1——
无约束回测会系统性高估可成交性（涨停可买、跌停可卖）。downgrade 恢复 '0'，
但不对既有数据回滚（无法区分用户显式关闭）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3e5f7a9b1d3'
down_revision: Union[str, Sequence[str], None] = 'b2d4e6f8a0c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('portfolio_strategies') as batch_op:
        batch_op.alter_column(
            'price_limit_check_enabled',
            existing_type=sa.Boolean(), nullable=False, server_default='1',
        )
    op.execute("UPDATE portfolio_strategies SET price_limit_check_enabled = 1")


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('portfolio_strategies') as batch_op:
        batch_op.alter_column(
            'price_limit_check_enabled',
            existing_type=sa.Boolean(), nullable=False, server_default='0',
        )
