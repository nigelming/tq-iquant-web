"""add IS/OOS gate columns to backtest_records

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-10-13 14:00:00.000000

C7 验证纪律层（默认关，关闭时行为与原逻辑一致）：
- backtest_records 增 is_oos_gate_enabled / oos_gate_min_ratio / gate_status /
  gate_reasons：启用后回测完成时按稳健性检验 oos 块评估
  OOS CAGR ≥ min_ratio × IS CAGR 且 OOS 不跑输基准，否则 gate_status=rejected
  （记录保留，仅标记不被采纳，审计友好）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4c5d6e7f8a9'
down_revision: Union[str, Sequence[str], None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'backtest_records',
        sa.Column('is_oos_gate_enabled', sa.Boolean(), server_default=sa.text('0'), nullable=False),
    )
    op.add_column(
        'backtest_records',
        sa.Column('oos_gate_min_ratio', sa.Numeric(4, 2), server_default='0.50', nullable=False),
    )
    op.add_column(
        'backtest_records',
        sa.Column('gate_status', sa.String(length=10), nullable=True),
    )
    op.add_column(
        'backtest_records',
        sa.Column('gate_reasons', sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('backtest_records', 'gate_reasons')
    op.drop_column('backtest_records', 'gate_status')
    op.drop_column('backtest_records', 'oos_gate_min_ratio')
    op.drop_column('backtest_records', 'is_oos_gate_enabled')
