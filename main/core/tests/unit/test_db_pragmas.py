"""db.py SQLite 连接 PRAGMA：WAL + busy_timeout（#9）。"""
from core.db import engine


def test_sqlite_pragmas_wal_and_busy_timeout():
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
        assert int(conn.exec_driver_sql("PRAGMA busy_timeout").scalar()) == 5000
        assert int(conn.exec_driver_sql("PRAGMA foreign_keys").scalar()) == 1
