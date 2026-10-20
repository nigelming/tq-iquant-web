"""add backtest_robustness_analyses table

Revision ID: f2a3b4c5d6e8
Revises: f7a8b9c0d1e2
Create Date: 2026-10-12 10:00:00.000000

回测稳健性检验结果（年度/3年滚动/个股集中度/样本内外对比）：镜像
backtest_market_regime_analyses 模式，1:1 关联回测记录、随父记录级联删除，
payload_json 存 analyze_robustness 完整 payload（JSON 安全值）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2a3b4c5d6e8'
down_revision: Union[str, Sequence[str], None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'backtest_robustness_analyses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('backtest_record_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('payload_json', sa.Text(), nullable=True),
        sa.Column('computed_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['backtest_record_id'], ['backtest_records.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('backtest_record_id', name='uq_backtest_robustness_record'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('backtest_robustness_analyses')
