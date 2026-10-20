"""add execution realism switches (price limit / volume limit / exposure cap)

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
Create Date: 2026-10-13 12:00:00.000000

回测成交现实性 3 项可选开关（全部默认关，关闭时行为与原逻辑一致）：
- portfolio_strategies.price_limit_check_enabled：#1 涨跌停约束——涨停买不进/
  跌停卖不出（成交步按前收×幅度判定，触发则延后重试）。旧组合迁移后保持关闭；
  新组合默认开启由创建 API 层设置（不动 ORM/server 默认，避免旧数据行为变化）。
- portfolio_strategies.volume_limit_enabled / volume_limit_ratio：#2 成交量/
  流动性约束——单 bar 成交量 × ratio 封顶成交股数（超限缩量，不足一手延后）。
- strategies.exposure_cap_enabled：#3 策略级敞口上限——回测 BUY 审批加卡
  策略总持仓市值 ≤ capital_ratio × 组合初始资金（仅回测成交步生效）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd6e7f8a9b0c1'
down_revision: Union[str, Sequence[str], None] = 'c5d6e7f8a9b0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'portfolio_strategies',
        sa.Column('price_limit_check_enabled', sa.Boolean(),
                  server_default=sa.text('0'), nullable=False),
    )
    op.add_column(
        'portfolio_strategies',
        sa.Column('volume_limit_enabled', sa.Boolean(),
                  server_default=sa.text('0'), nullable=False),
    )
    op.add_column(
        'portfolio_strategies',
        sa.Column('volume_limit_ratio', sa.Numeric(4, 2),
                  server_default='0.10', nullable=False),
    )
    op.add_column(
        'strategies',
        sa.Column('exposure_cap_enabled', sa.Boolean(),
                  server_default=sa.text('0'), nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('strategies', 'exposure_cap_enabled')
    op.drop_column('portfolio_strategies', 'volume_limit_ratio')
    op.drop_column('portfolio_strategies', 'volume_limit_enabled')
    op.drop_column('portfolio_strategies', 'price_limit_check_enabled')
