"""add adaptive_slippage_enabled + slippage_impact_coef to portfolio_strategies

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-10-14 14:00:00.000000

自适应滑点（回测成交步，可选默认关）：有效滑点 = 基础滑点 + 冲击系数 ×
订单股数/(bar成交量×100)，封顶 0.10，模拟市场冲击成本。与 volume_limit 协同。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'portfolio_strategies',
        sa.Column('adaptive_slippage_enabled', sa.Boolean(),
                  server_default='0', nullable=False),
    )
    op.add_column(
        'portfolio_strategies',
        sa.Column('slippage_impact_coef', sa.Numeric(6, 4),
                  server_default='0.1000', nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('portfolio_strategies', 'slippage_impact_coef')
    op.drop_column('portfolio_strategies', 'adaptive_slippage_enabled')
