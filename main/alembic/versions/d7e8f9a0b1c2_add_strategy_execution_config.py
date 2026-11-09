"""add strategy execution config (requote / split order / risk timing)

Revision ID: d7e8f9a0b1c2
Revises: c3d9e7f1a5b8
Create Date: 2026-10-29 10:00:00.000000

批次1 策略级实盘执行配置（全部向后兼容：server_default 兜底旧行为）：
- requote_enabled / requote_max_attempts：撤单后有限追价重下（默认关 / 2 次）
- split_order_enabled / split_order_threshold：TDX 大额拆单（默认关 / 50 万，
  本批次仅落配置管线，拆单逻辑批次 2 实现）
- risk_timing：风控/信号执行时点（next_day=当日信号次日执行，默认 /
  immediate=当日确认立即执行）
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7e8f9a0b1c2'
down_revision: Union[str, Sequence[str], None] = 'c3d9e7f1a5b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('strategies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('requote_enabled', sa.Boolean(),
                                      nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('requote_max_attempts', sa.Integer(),
                                      nullable=False, server_default='2'))
        batch_op.add_column(sa.Column('split_order_enabled', sa.Boolean(),
                                      nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('split_order_threshold', sa.Float(),
                                      nullable=False, server_default='500000.0'))
        batch_op.add_column(sa.Column('risk_timing', sa.String(15),
                                      nullable=False, server_default='next_day'))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('strategies', schema=None) as batch_op:
        batch_op.drop_column('risk_timing')
        batch_op.drop_column('split_order_threshold')
        batch_op.drop_column('split_order_enabled')
        batch_op.drop_column('requote_max_attempts')
        batch_op.drop_column('requote_enabled')
