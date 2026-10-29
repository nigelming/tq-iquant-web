"""批次1：策略实盘执行配置迁移 up-down 集成测试。

真实跑 Alembic 迁移链（tmp SQLite，经 monkeypatch core.config.load_config
指向临时库——env.py 每次命令重新执行 from-import，取到补丁后的 load_config）：
upgrade head → strategies 含 5 个新列；downgrade 到前一 head（c3d9e7f1a5b8）→ 列移除。
"""
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

_MAIN_DIR = Path(__file__).resolve().parents[3]

NEW_COLUMNS = {
    "requote_enabled",
    "requote_max_attempts",
    "split_order_enabled",
    "split_order_threshold",
    "risk_timing",
}
_PREV_HEAD = "c3d9e7f1a5b8"


@pytest.fixture
def alembic_cfg(tmp_path, monkeypatch):
    """指向 tmp SQLite 的 Alembic Config（load_config 打补丁喂临时库路径）。

    monkeypatch 掉 logging.config.fileConfig：env.py 每次跑迁移都会 fileConfig
    重配置日志（disable_existing_loggers 默认 True）→ 全进程既有 logger 被
    disable，污染同进程后续单测的 caplog 断言。测试不需要 alembic 日志配置。
    """
    monkeypatch.setattr(
        "logging.config.fileConfig", lambda *a, **kw: None
    )
    db_file = tmp_path / "migration.db"
    monkeypatch.setattr(
        "core.config.load_config",
        lambda: {"database": {"sqlite_path": str(db_file)}},
    )
    return Config(str(_MAIN_DIR / "alembic.ini"))


def _strategy_columns(cfg):
    """跑 upgrade head 后读 strategies 表列名（独立连接读 tmp 库）。"""
    from core.config import load_config
    sqlite_path = load_config()["database"]["sqlite_path"]
    engine = create_engine(f"sqlite:///{sqlite_path}")
    try:
        return {c["name"] for c in inspect(engine).get_columns("strategies")}
    finally:
        engine.dispose()


def test_upgrade_head_adds_execution_config_columns(alembic_cfg):
    command.upgrade(alembic_cfg, "head")
    cols = _strategy_columns(alembic_cfg)
    assert NEW_COLUMNS <= cols


def test_downgrade_prev_head_removes_execution_config_columns(alembic_cfg):
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, _PREV_HEAD)
    cols = _strategy_columns(alembic_cfg)
    assert not (NEW_COLUMNS & cols)
