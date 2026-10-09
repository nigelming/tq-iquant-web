"""drop dead progress column from backtest_records

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-10-09 18:00:00.000000

progress 死字段移除（#10）：回测为同步内联执行（请求线程内跑完），progress
永远只能在起始写 0、结束写 100，运行中无任何中间值可写——字段无信息量且误导
（前端展示恒为 0% 或 100%）。回测状态以 status（running/completed/failed）为准。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f7a8b9c0d1e2'
down_revision: Union[str, Sequence[str], None] = 'e6f7a8b9c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema. SQLite 用 batch mode。"""
    with op.batch_alter_table('backtest_records', schema=None) as batch_op:
        batch_op.drop_column('progress')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('backtest_records', schema=None) as batch_op:
        batch_op.add_column(sa.Column('progress', sa.Integer(), nullable=False,
                                      server_default='0'))
