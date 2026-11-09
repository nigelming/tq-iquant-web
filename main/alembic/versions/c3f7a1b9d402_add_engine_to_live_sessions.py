"""add engine to live_sessions

Revision ID: c3f7a1b9d402
Revises: 02ff516824cc
Create Date: 2026-10-24

实盘交易引擎按会话可选：iquant / tdx(通达信TQ) / ths(同花顺UI) / qmt(miniQMT)。
存量会话留 NULL，运行时按全局默认引擎解析（升级前 ths_ui.enabled 行为不变）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3f7a1b9d402'
down_revision: Union[str, Sequence[str], None] = '02ff516824cc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('live_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('engine', sa.String(10), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('live_sessions', schema=None) as batch_op:
        batch_op.drop_column('engine')
