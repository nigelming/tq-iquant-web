"""add backtest_market_regime_analyses table

Revision ID: c9d0e1f2a3b4
Revises: b7c8d9e0f1a2
Create Date: 2026-09-27 10:00:00.000000

市场状态×绩效分析结果（regime-alpha 套件阶段二，FR-11）：镜像
backtest_crisis_alphas 模式，1:1 关联回测记录、随父记录级联删除，
payload_json 存 RegimeAlphaAnalyzer 完整 payload（JSON 安全值）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, Sequence[str], None] = 'b7c8d9e0f1a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'backtest_market_regime_analyses',
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
        sa.UniqueConstraint('backtest_record_id', name='uq_backtest_market_regime_record'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('backtest_market_regime_analyses')
