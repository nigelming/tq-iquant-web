"""add backtest_records.validation_json (capacity + crisis + oos verdict)

Revision ID: c1d2e3f4a5b6
Revises: e3e2d1c0b9a8
Create Date: 2026-10-14 12:00:00.000000

验证结论（JSON dict）冻结：容量健康自动预警 + 危机防御机制逐窗判定 +
样本外(walk-forward)引用摘要。回测完成时写入，随记录审计。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'e3e2d1c0b9a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('backtest_records', sa.Column('validation_json', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('backtest_records', 'validation_json')
