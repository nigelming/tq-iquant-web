"""db.py 连接 PRAGMA 测试（#9：并发下 SQLite 写锁保护）。

用独立临时文件 SQLite 引擎挂同一连接监听器验证，不触碰真实 dev.db。
（内存库不支持 WAL —— journal_mode 恒为 memory，必须用文件库验证。）
"""
from sqlalchemy import create_engine, event

from core.db import _enable_sqlite_fk


def test_sqlite_pragmas_on_connect(tmp_path):
    """每连接开启外键约束 + WAL 日志模式 + busy_timeout（写锁等待 5s）。"""
    eng = create_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    event.listen(eng, "connect", _enable_sqlite_fk)
    with eng.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert str(conn.exec_driver_sql("PRAGMA journal_mode").scalar()).lower() == "wal"
        assert conn.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
