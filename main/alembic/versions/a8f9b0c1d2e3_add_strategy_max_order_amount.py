"""add strategy max_order_amount (单笔最大买入金额)

Revision ID: a8f9b0c1d2e3
Revises: d7e8f9a0b1c2
Create Date: 2026-10-29 22:00:00.000000

策略级单笔最大买入金额（元，nullable，None=不限）：BUY 审批时金额超此值
→ 缩量到该金额能买的整手股数。回测/实盘双端生效（同一 cap_quantity）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a8f9b0c1d2e3'
down_revision: Union[str, Sequence[str], None] = 'd7e8f9a0b1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('strategies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_order_amount', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('strategies', schema=None) as batch_op:
        batch_op.drop_column('max_order_amount')
