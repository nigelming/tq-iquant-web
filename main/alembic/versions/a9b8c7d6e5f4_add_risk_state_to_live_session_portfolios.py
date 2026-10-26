"""add risk_state to live_session_portfolios

Revision ID: a9b8c7d6e5f4
Revises: d1e2f3a4b5c6
Create Date: 2026-10-25 22:40:00.000000

实盘风控内存态快照（JSON 文本）：peak_value / prev_close_value /
daily_pause_active / breaker_trigger_date / daily_loss_trigger_date。

背景：这些状态原本只存在进程内存里，Core 重启后：
- peak_value 从重启首根 bar 重建 → 历史峰值被抹平，最大回撤熔断少触发；
- prev_close_value 丢失 → 重启当日日内亏损无法按昨收净值判定；
- daily_pause_active 丢失 → 已触发的日内暂停被意外解除。

日终（14:30）与熔断触发/手动恢复时落库，启动时读回。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a9b8c7d6e5f4'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema. SQLite 用 batch mode。"""
    with op.batch_alter_table('live_session_portfolios', schema=None) as batch_op:
        batch_op.add_column(sa.Column('risk_state', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('live_session_portfolios', schema=None) as batch_op:
        batch_op.drop_column('risk_state')
