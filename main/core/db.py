from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from core.config import load_config

_cfg = load_config()
_sqlite_path = _cfg.get("database", {}).get("sqlite_path", "dev.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///./{_sqlite_path}"

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
