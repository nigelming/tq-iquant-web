from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from core.config import load_config

# 锚定到 main/ 目录（本文件位于 main/core/db.py），让 sqlite_path 无论
# 从哪个 cwd 启动都解析到同一绝对路径，消除对进程工作目录的依赖。
_MAIN_DIR = Path(__file__).resolve().parent.parent

_cfg = load_config()
_sqlite_path = _cfg.get("database", {}).get("sqlite_path", "data/dev.db")
# 相对路径锚定到 main/；若 config 给绝对路径则原样使用
_db_file = Path(_sqlite_path)
if not _db_file.is_absolute():
    _db_file = _MAIN_DIR / _sqlite_path
SQLALCHEMY_DATABASE_URL = f"sqlite:///{_db_file.as_posix()}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)
SessionLocal = sessionmaker(bind=engine)


@event.listens_for(engine, "connect")
def _enable_sqlite_fk(dbapi_conn, connection_record):
    """SQLite 外键约束默认关闭，逐连接开启以让 ondelete CASCADE/RESTRICT 生效。"""
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_db():
    from core.models import Base

    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
