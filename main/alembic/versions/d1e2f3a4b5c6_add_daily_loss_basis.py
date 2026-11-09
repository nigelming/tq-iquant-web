"""add daily_loss_basis to portfolio_strategies

Revision ID: d1e2f3a4b5c6
Revises: c3f7a1b9d402
Create Date: 2026-10-25 20:00:00.000000

日内亏损熔断（daily_loss_limit）的分母口径：
- equity（默认）：按「上一交易日收盘净值」× limit，阈值随净值滚动（修正口径）；
- initial：按 initial_capital × limit，阈值恒定（旧行为，净值增长后相对变严）。
存量组合统一置 equity，即默认采用修正口径。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c3f7a1b9d402'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema. SQLite 用 batch mode。"""
    with op.batch_alter_table('portfolio_strategies', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'daily_loss_basis', sa.String(length=10),
            nullable=False, server_default='equity',
        ))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('portfolio_strategies', schema=None) as batch_op:
        batch_op.drop_column('daily_loss_basis')
