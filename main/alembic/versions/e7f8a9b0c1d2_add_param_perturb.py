"""add param_perturb_runs and backtest_records.param_perturb_run_id

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
Create Date: 2026-10-13 18:00:00.000000

P2b 验证纪律层（新功能，不影响现有回测行为）：
- 新表 param_perturb_runs：对子策略数值参数逐一 ±perturb_pct 扰动各跑一
  轮子回测 + 1 轮基准，summary_json 落各轮指标与最敏感参数。
- backtest_records 增 param_perturb_run_id：标记微扰子回测，
  列表接口默认过滤（NULL = 普通回测，历史行为不变）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7f8a9b0c1d2'
down_revision: Union[str, Sequence[str], None] = 'd6e7f8a9b0c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'param_perturb_runs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('portfolio_strategy_id', sa.Integer(),
                  sa.ForeignKey('portfolio_strategies.id', ondelete='RESTRICT'),
                  nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('start_date', sa.Date(), nullable=False),
        sa.Column('end_date', sa.Date(), nullable=False),
        sa.Column('perturb_pct', sa.Numeric(4, 2), server_default='0.10', nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('summary_json', sa.Text(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now()),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_param_perturb_runs_portfolio_strategy_id',
                    'param_perturb_runs', ['portfolio_strategy_id'])
    # SQLite 无法用 ALTER 补 FK 约束：仅加列（FK 由模型层声明，SQLite 默认不强制）
    op.add_column(
        'backtest_records',
        sa.Column('param_perturb_run_id', sa.Integer(), nullable=True),
    )
    op.create_index('ix_backtest_records_param_perturb_run_id',
                    'backtest_records', ['param_perturb_run_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_backtest_records_param_perturb_run_id', table_name='backtest_records')
    op.drop_column('backtest_records', 'param_perturb_run_id')
    op.drop_index('ix_param_perturb_runs_portfolio_strategy_id', table_name='param_perturb_runs')
    op.drop_table('param_perturb_runs')
