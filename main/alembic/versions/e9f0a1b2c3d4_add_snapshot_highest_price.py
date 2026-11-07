"""add highest_price to live account position snapshots

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-11-03 21:00:00.000000

移动止损峰值持久化：快照持仓行记录 highest_price，外部持仓导入时恢复
（否则重启后移动止损基准从启动价重新起算，可能延迟触发）。
"""
from alembic import op
import sqlalchemy as sa


revision = "e9f0a1b2c3d4"
down_revision = "d8e9f0a1b2c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("live_account_position_snapshots") as batch:
        batch.add_column(
            sa.Column("highest_price", sa.Numeric(10, 4), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("live_account_position_snapshots") as batch:
        batch.drop_column("highest_price")
