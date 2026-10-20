"""add missing FKs: backtest_records walk/perturb run_id + strategies.stock_pool_id

Revision ID: f9a8b7c6d5e4
Revises: c3e5f7a9b1d3
Create Date: 2026-10-13 20:00:00.000000

审计修复：模型层声明的三个外键约束在迁移链里缺失（仅加列/索引），
而运行期已启用 PRAGMA foreign_keys=ON（core/db.py），导致删除行为与模型声明
不一致且 alembic check 报 schema drift：
- backtest_records.walk_forward_run_id  → walk_forward_runs.id  (SET NULL)
- backtest_records.param_perturb_run_id → param_perturb_runs.id (SET NULL)
- strategies.stock_pool_id              → stock_pools.id       (RESTRICT)

SQLite 用 batch_alter_table 重建表补 FK（render_as_batch 已配），
数据原样保留（已确认无孤儿引用）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f9a8b7c6d5e4'
down_revision: Union[str, Sequence[str], None] = 'c3e5f7a9b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema: add the three missing FK constraints."""
    with op.batch_alter_table('backtest_records') as batch_op:
        batch_op.create_foreign_key(
            'fk_backtest_records_walk_forward_run_id_walk_forward_runs',
            'walk_forward_runs', ['walk_forward_run_id'], ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_foreign_key(
            'fk_backtest_records_param_perturb_run_id_param_perturb_runs',
            'param_perturb_runs', ['param_perturb_run_id'], ['id'],
            ondelete='SET NULL',
        )
    with op.batch_alter_table('strategies') as batch_op:
        batch_op.create_foreign_key(
            'fk_strategies_stock_pool_id_stock_pools',
            'stock_pools', ['stock_pool_id'], ['id'],
            ondelete='RESTRICT',
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('backtest_records') as batch_op:
        batch_op.drop_constraint('fk_backtest_records_walk_forward_run_id_walk_forward_runs',
                                 type_='foreignkey')
        batch_op.drop_constraint('fk_backtest_records_param_perturb_run_id_param_perturb_runs',
                                 type_='foreignkey')
    with op.batch_alter_table('strategies') as batch_op:
        batch_op.drop_constraint('fk_strategies_stock_pool_id_stock_pools',
                                 type_='foreignkey')
