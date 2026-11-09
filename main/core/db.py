import sys
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from core.config import app_root, is_frozen_app, load_config, use_app_root

# 锚定到 main/ 目录（本文件位于 main/core/db.py），让 sqlite_path 无论
# 从哪个 cwd 启动都解析到同一绝对路径，消除对进程工作目录的依赖。
_MAIN_DIR = Path(__file__).resolve().parent.parent


def _db_path() -> Path:
    """数据库路径：优先 config.yaml，fallback 到应用根目录（可写）。"""
    cfg = load_config()
    sqlite_path = cfg.get("database", {}).get("sqlite_path", "data/dev.db")
    p = Path(sqlite_path)
    if not p.is_absolute():
        # 打包版锚到 app_root()：安装目录不可写时自动落到 %LOCALAPPDATA%，
        # 避免 SQLite 在只读目录下建库失败。
        p = (app_root() if use_app_root() else _MAIN_DIR) / p
    return p


_db_file = _db_path()
SQLALCHEMY_DATABASE_URL = f"sqlite:///{_db_file.as_posix()}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 5},
)
SessionLocal = sessionmaker(bind=engine)


@event.listens_for(engine, "connect")
def _enable_sqlite_fk(dbapi_conn, connection_record):
    """SQLite 逐连接 PRAGMA：外键约束 + WAL 日志 + 写锁等待（#9）。

    foreign_keys=ON 让 ondelete CASCADE/RESTRICT 生效（默认关闭）；
    journal_mode=WAL 读写不互斥，SSE 推送/轮询读与回测写并发时不再互相阻塞；
    busy_timeout=5000ms 写锁冲突时等待 5s 再报 database is locked。
    """
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


def init_db():
    """应用数据库迁移（Alembic upgrade head），替代原 Base.metadata.create_all。

    审计 #22：schema 变更必须全部走迁移链而非旁路建表，避免 create_all 与迁移链漂移。
    已迁移到 head 的库是 no-op；迁移失败则抛异常阻止应用启动（fail fast）。

    注意：刻意不加载 alembic.ini —— 否则 env.py 会执行 fileConfig()，其默认
    disable_existing_loggers=True 会禁用 core.* 等业务 logger，污染运行期日志与测试 caplog。
    用无参 Config + 绝对路径显式指定 script_location/prepend_sys_path；
    url 由 alembic/env.py 的 _db_url() 从 config.yaml 读取并锚定 main/（与 db.py 同源），
    因此无需 ini 的其余配置。
    """
    import sys

    from alembic import command
    from alembic.config import Config

    # 手动把 main/ 加入 sys.path：不依赖 alembic 的 prepend_sys_path（无 ini 时其
    # legacy 分割会把 Windows 盘符 D: 误切，见 alembic DeprecationWarning）。
    if str(_MAIN_DIR) not in sys.path:
        sys.path.insert(0, str(_MAIN_DIR))

    cfg = Config()
    script_dir = _MAIN_DIR / "alembic"
    if is_frozen_app():
        # 打包版：迁移脚本随安装目录分发（不是数据目录），优先取 exe 同级 alembic/
        packaged = Path(sys.argv[0]).resolve().parent / "alembic"
        if (packaged / "env.py").exists():
            script_dir = packaged
    cfg.set_main_option("script_location", str(script_dir))
    command.upgrade(cfg, "head")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
