"""add live signal alert tables and columns

Revision ID: f0a1b2c3d4e5
Revises: a9b8c7d6e5f4
Create Date: 2026-10-26 10:00:00.000000

实盘信号预警（alert 模式）：新增 live_signals 记录公式信号；
live_sessions.execution_mode 区分 alert/auto；strategies 增加信号推送调度列。
"""
from alembic import op
import sqlalchemy as sa


revision = "f0a1b2c3d4e5"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "live_sessions",
        sa.Column("execution_mode", sa.String(10), nullable=False,
                  server_default="auto"),
    )
    for name, typ in (
        ("signal_frequency", sa.String(10)),
        ("signal_at", sa.String(5)),
        ("signal_weekday", sa.Integer()),
        ("signal_data_end", sa.String(10)),
    ):
        op.add_column("strategies", sa.Column(name, typ, nullable=True))
    op.create_table(
        "live_signals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("live_session_id", sa.Integer(),
                  sa.ForeignKey("live_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("portfolio_strategy_id", sa.Integer(),
                  sa.ForeignKey("portfolio_strategies.id"), nullable=False),
        sa.Column("strategy_id", sa.Integer(),
                  sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("formula_id", sa.Integer(),
                  sa.ForeignKey("formulas.id"), nullable=True),
        sa.Column("stock_code", sa.String(20), nullable=False),
        sa.Column("period", sa.String(5), nullable=False),
        sa.Column("bar_time", sa.DateTime(), nullable=False),
        sa.Column("formula_name", sa.String(100), nullable=False),
        sa.Column("signal_name", sa.String(50), nullable=False),
        sa.Column("signal_type", sa.String(10), nullable=False),
        sa.Column("trigger_value", sa.Integer(), nullable=False),
        sa.Column("price", sa.Numeric(10, 3), nullable=True),
        sa.Column("execution_mode", sa.String(10), nullable=False),
        sa.Column("status", sa.String(15), nullable=False,
                  server_default="new"),
        sa.Column("alert_status", sa.String(15), nullable=True),
        sa.Column("alert_error", sa.String(500), nullable=True),
        sa.Column("tdx_run_id", sa.String(30), nullable=True),
        sa.Column("alerted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "live_session_id", "portfolio_strategy_id", "strategy_id",
            "stock_code", "signal_name", "bar_time",
            name="uq_live_signal_dedup",
        ),
    )
    op.create_index("ix_live_signals_live_session_id", "live_signals",
                    ["live_session_id"])
    op.create_index("ix_live_signals_portfolio_strategy_id", "live_signals",
                    ["portfolio_strategy_id"])
    op.create_index("ix_live_signals_strategy_id", "live_signals",
                    ["strategy_id"])
    op.create_index("ix_live_signals_session_bar", "live_signals",
                    ["live_session_id", "bar_time"])
    op.create_index("ix_live_signals_strategy_stock", "live_signals",
                    ["strategy_id", "stock_code"])


def downgrade() -> None:
    for ix in (
        "ix_live_signals_strategy_stock", "ix_live_signals_session_bar",
        "ix_live_signals_strategy_id", "ix_live_signals_portfolio_strategy_id",
        "ix_live_signals_live_session_id",
    ):
        op.drop_index(ix, table_name="live_signals")
    op.drop_table("live_signals")
    with op.batch_alter_table("strategies") as batch:
        batch.drop_column("signal_data_end")
        batch.drop_column("signal_weekday")
        batch.drop_column("signal_at")
        batch.drop_column("signal_frequency")
    with op.batch_alter_table("live_sessions") as batch:
        batch.drop_column("execution_mode")
