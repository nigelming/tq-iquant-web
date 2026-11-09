"""外部信号注入：live_sessions 增 signal_file_path

Revision ID: b1c2d3e4f5a6
Revises: a8f9b0c1d2e3
Create Date: 2026-10-30

外部信号注入链路（0011）：同花顺条件预警动态输出文本文件 → 平台增量读取 →
公式名匹配策略 → 注入 signal_cache 走 Portfolio.on_bar 全套。会话级开关：
signal_file_path 为 NULL 时行为不变（不启用）；配置后引擎定时增量读取该文件。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, Sequence[str], None] = 'a8f9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('live_sessions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('signal_file_path', sa.String(500), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('live_sessions', schema=None) as batch_op:
        batch_op.drop_column('signal_file_path')
