"""add walk_forward_runs and backtest_records.walk_forward_run_id

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
Create Date: 2026-10-13 15:00:00.000000

C8 验证纪律层（新功能，不影响现有回测行为）：
- 新表 walk_forward_runs：滚动 train_years 年训练 + test_years 年验证逐段前移，
  每窗子回测过 IS/OOS 门槛，summary_json 落各窗明细与 overall_pass。
- backtest_records 增 walk_forward_run_id：标记窗口子回测，
  列表接口默认过滤（NULL = 普通回测，历史行为不变）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5d6e7f8a9b0'
down_revision: Union[str, Sequence[str], None] = 'b4c5d6e7f8a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'walk_forward_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('portfolio_strategy_id', sa.Integer(),
                  sa.ForeignKey('portfolio_strategies.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('train_years', sa.Integer(), server_default='3', nullable=False),
        sa.Column('test_years', sa.Integer(), server_default='1', nullable=False),
        sa.Column('oos_gate_min_ratio', sa.Numeric(4, 2), server_default='0.50', nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('summary_json', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_walk_forward_runs_portfolio_strategy_id',
                    'walk_forward_runs', ['portfolio_strategy_id'])
    # SQLite 无法用 ALTER 补 FK 约束：仅加列（FK 由模型层声明，SQLite 默认不强制）
    op.add_column(
        'backtest_records',
        sa.Column('walk_forward_run_id', sa.Integer(), nullable=True),
    )
    op.create_index('ix_backtest_records_walk_forward_run_id',
                    'backtest_records', ['walk_forward_run_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_backtest_records_walk_forward_run_id', table_name='backtest_records')
    op.drop_column('backtest_records', 'walk_forward_run_id')
    op.drop_index('ix_walk_forward_runs_portfolio_strategy_id', table_name='walk_forward_runs')
    op.drop_table('walk_forward_runs')
