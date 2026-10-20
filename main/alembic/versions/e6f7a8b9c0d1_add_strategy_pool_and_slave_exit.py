"""add stock_pool_id and slave_exit_mode to strategies

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-10-09 15:00:00.000000

策略级股票池（stock_pool_id）+ 从策略卖出模式（slave_exit_mode）：
- stock_pool_id INTEGER NULL：空=继承组合级 stock_pool_id；非空=策略独立股票池。
  FK 约束由应用层 strategy_service 校验（SQLite batch ALTER + FK 有循环依赖）。
- slave_exit_mode VARCHAR(15) NOT NULL DEFAULT 'independent'：
  仅 role=slave 时有意义；follow_master 模式下 master 卖出时 slave 同步全平。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, Sequence[str], None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema. SQLite 用 batch mode。"""
    with op.batch_alter_table('strategies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('stock_pool_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('slave_exit_mode', sa.String(length=15),
                                      nullable=False, server_default='independent'))
        batch_op.create_index('ix_strategies_stock_pool_id', ['stock_pool_id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('strategies', schema=None) as batch_op:
        batch_op.drop_index('ix_strategies_stock_pool_id')
        batch_op.drop_column('slave_exit_mode')
        batch_op.drop_column('stock_pool_id')
