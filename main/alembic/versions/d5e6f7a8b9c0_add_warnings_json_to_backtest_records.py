"""add warnings_json to backtest_records

Revision ID: d5e6f7a8b9c0
Revises: b2c3d4e5f6a7
Create Date: 2026-10-09 14:40:00.000000

信号预检：回测 completed 但公式报错/输出线缺失/区间内零信号时，
以 JSON list[str] 落非致命警告，供详情页置顶提示。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'backtest_records',
        sa.Column('warnings_json', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('backtest_records', 'warnings_json')
