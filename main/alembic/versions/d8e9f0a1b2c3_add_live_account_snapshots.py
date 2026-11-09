"""add live account snapshots (account alignment)

Revision ID: d8e9f0a1b2c3
Revises: f0a1b2c3d4e5
Create Date: 2026-11-03 15:30:00.000000

实盘账户全貌对齐：live_account_snapshots（总资产/可用资金/市值）+
live_account_position_snapshots（持仓明细：数量/可用/成本/现价/市值）。
用途：BUY 资金审批对齐真实可用资金、外部持仓导入虚拟账本纳入风控、
工作台账户全貌展示。
"""
from alembic import op
import sqlalchemy as sa


revision = "d8e9f0a1b2c3"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "live_account_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("live_session_id", sa.Integer(),
                  sa.ForeignKey("live_sessions.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("engine", sa.String(15), nullable=False),
        sa.Column("captured_at", sa.DateTime(), nullable=False),
        sa.Column("total_asset", sa.Numeric(16, 2), nullable=True),
        sa.Column("available_cash", sa.Numeric(16, 2), nullable=True),
        sa.Column("market_value", sa.Numeric(16, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index("ix_live_account_snapshots_live_session_id",
                    "live_account_snapshots", ["live_session_id"])
    op.create_index("ix_live_account_snapshots_captured_at",
                    "live_account_snapshots", ["captured_at"])
    op.create_index("ix_live_account_snapshots_session_time",
                    "live_account_snapshots", ["live_session_id", "captured_at"])
    op.create_table(
        "live_account_position_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("snapshot_id", sa.Integer(),
                  sa.ForeignKey("live_account_snapshots.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("stock_code", sa.String(20), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("available_quantity", sa.Integer(), nullable=True),
        sa.Column("cost_price", sa.Numeric(10, 4), nullable=True),
        sa.Column("current_price", sa.Numeric(10, 4), nullable=True),
        sa.Column("market_value", sa.Numeric(16, 2), nullable=True),
    )
    op.create_index("ix_live_account_position_snapshots_snapshot_id",
                    "live_account_position_snapshots", ["snapshot_id"])
    op.create_index("ix_live_account_position_snapshots_stock_code",
                    "live_account_position_snapshots", ["stock_code"])


def downgrade() -> None:
    op.drop_index("ix_live_account_position_snapshots_stock_code",
                  table_name="live_account_position_snapshots")
    op.drop_index("ix_live_account_position_snapshots_snapshot_id",
                  table_name="live_account_position_snapshots")
    op.drop_table("live_account_position_snapshots")
    op.drop_index("ix_live_account_snapshots_session_time",
                  table_name="live_account_snapshots")
    op.drop_index("ix_live_account_snapshots_captured_at",
                  table_name="live_account_snapshots")
    op.drop_index("ix_live_account_snapshots_live_session_id",
                  table_name="live_account_snapshots")
    op.drop_table("live_account_snapshots")
