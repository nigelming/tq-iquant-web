"""add no_leak_cross to formulas

Revision ID: c0d1e2f3a4b5
Revises: b8d9e0f1a2b3
Create Date: 2026-10-20

批次 9a：跨周期引用（C#WEEK）公式级无泄露开关，默认 false=TDX 语义
（所属上级 bar 未完成时引用其整根终值，周内含未来引用）；仅影响
engine=builtin 回测链路。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c0d1e2f3a4b5'
down_revision: Union[str, Sequence[str], None] = 'b8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('formulas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('no_leak_cross', sa.Boolean(),
                                      server_default='0', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('formulas', schema=None) as batch_op:
        batch_op.drop_column('no_leak_cross')
