"""add decision event tables (backtest_decision_events / live_decision_events)

Revision ID: e5f6a7b8c9d0
Revises: d4a5b6c7d8e9
Create Date: 2026-08-28 10:00:00.000000

风控/决策闸门可观测性：信号→成交链路上每个闸门触发（风控止损/熔断/丢单/拒单/
缩量/压制）逐事件落一行，回测、实盘各一张表，随父记录级联删除。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, Sequence[str], None] = 'd4a5b6c7d8e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'backtest_decision_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('backtest_record_id', sa.Integer(), nullable=False),
        sa.Column('gate', sa.String(length=40), nullable=False),
        sa.Column('layer', sa.String(length=20), nullable=False),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('portfolio_id', sa.Integer(), nullable=True),
        sa.Column('strategy_id', sa.Integer(), nullable=True),
        sa.Column('stock_code', sa.String(length=20), nullable=True),
        sa.Column('bar_time', sa.DateTime(), nullable=True),
        sa.Column('param_name', sa.String(length=40), nullable=True),
        sa.Column('param_value', sa.Float(), nullable=True),
        sa.Column('actual_value', sa.Float(), nullable=True),
        sa.Column('requested_qty', sa.Integer(), nullable=True),
        sa.Column('final_qty', sa.Integer(), nullable=True),
        sa.Column('message', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['backtest_record_id'], ['backtest_records.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_bt_decisions_rec_gate', 'backtest_decision_events',
                    ['backtest_record_id', 'gate'], unique=False)
    op.create_index('ix_bt_decisions_rec_bar', 'backtest_decision_events',
                    ['backtest_record_id', 'bar_time'], unique=False)
    op.create_index('ix_backtest_decision_events_strategy_id', 'backtest_decision_events',
                    ['strategy_id'], unique=False)

    op.create_table(
        'live_decision_events',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('live_session_id', sa.Integer(), nullable=False),
        sa.Column('gate', sa.String(length=40), nullable=False),
        sa.Column('layer', sa.String(length=20), nullable=False),
        sa.Column('action', sa.String(length=20), nullable=False),
        sa.Column('portfolio_id', sa.Integer(), nullable=True),
        sa.Column('strategy_id', sa.Integer(), nullable=True),
        sa.Column('stock_code', sa.String(length=20), nullable=True),
        sa.Column('bar_time', sa.DateTime(), nullable=True),
        sa.Column('param_name', sa.String(length=40), nullable=True),
        sa.Column('param_value', sa.Float(), nullable=True),
        sa.Column('actual_value', sa.Float(), nullable=True),
        sa.Column('requested_qty', sa.Integer(), nullable=True),
        sa.Column('final_qty', sa.Integer(), nullable=True),
        sa.Column('message', sa.String(length=200), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=True),
        sa.ForeignKeyConstraint(['live_session_id'], ['live_sessions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_live_decisions_sess_gate', 'live_decision_events',
                    ['live_session_id', 'gate'], unique=False)
    op.create_index('ix_live_decisions_sess_bar', 'live_decision_events',
                    ['live_session_id', 'bar_time'], unique=False)
    op.create_index('ix_live_decision_events_strategy_id', 'live_decision_events',
                    ['strategy_id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_live_decision_events_strategy_id', table_name='live_decision_events')
    op.drop_index('ix_live_decisions_sess_bar', table_name='live_decision_events')
    op.drop_index('ix_live_decisions_sess_gate', table_name='live_decision_events')
    op.drop_table('live_decision_events')
    op.drop_index('ix_backtest_decision_events_strategy_id', table_name='backtest_decision_events')
    op.drop_index('ix_bt_decisions_rec_bar', table_name='backtest_decision_events')
    op.drop_index('ix_bt_decisions_rec_gate', table_name='backtest_decision_events')
    op.drop_table('backtest_decision_events')
