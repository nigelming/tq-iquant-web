"""add backtest_crisis_alphas table

Revision ID: f2a3b4c5d6e7
Revises: e5f6a7b8c9d0
Create Date: 2026-09-25 10:00:00.000000

危机Alpha 专题分析结果：回测结束后以独立事务落库，1:1 关联回测记录，
随父记录级联删除。payload_json 存 12 模块完整结果（已净化为 JSON 安全值）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, Sequence[str], None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'backtest_crisis_alphas',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('backtest_record_id', sa.Integer(), nullable=False),
        sa.Column('benchmark_code', sa.String(length=20), nullable=False),
        sa.Column('benchmark_name', sa.String(length=20), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('skipped_benchmarks_json', sa.Text(), nullable=True),
        sa.Column('payload_json', sa.Text(), nullable=True),
        sa.Column('computed_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['backtest_record_id'], ['backtest_records.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('backtest_record_id', name='uq_backtest_crisis_alphas_record'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('backtest_crisis_alphas')
