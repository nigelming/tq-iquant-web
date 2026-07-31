"""add code to stock_pools

Revision ID: a1b2c3d4e5f6
Revises: 185ad90e988e
Create Date: 2026-07-31 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = '185ad90e988e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """加 stock_pools.code 列（通达信板块 Code，NOT NULL）。

    dev.db 可清空：联调前清 stock_pools + stock_pool_stocks，无存量回填需求。
    新列放在 name 之前（与模型列顺序一致），SQLite batch_alter_table 支持位置参数。
    """
    with op.batch_alter_table('stock_pools', schema=None) as batch_op:
        batch_op.add_column(sa.Column('code', sa.String(length=50), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table('stock_pools', schema=None) as batch_op:
        batch_op.drop_column('code')
