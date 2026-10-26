"""add engine to formulas

Revision ID: b8d9e0f1a2b3
Revises: d2e3f4a5b6c7
Create Date: 2026-10-17

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8d9e0f1a2b3'
down_revision: Union[str, Sequence[str], None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('formulas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('engine', sa.String(10), server_default='tq', nullable=False))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('formulas', schema=None) as batch_op:
        batch_op.drop_column('engine')
