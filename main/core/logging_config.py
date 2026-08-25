import logging
import logging.handlers
import os
import sys
from pathlib import Path


def _under_pytest() -> bool:
    """是否在 pytest 测试进程内。测试期间不挂 FileHandler——否则单测日志会写进 core.log
    污染真实日志；且 core 进程占用 core.log 时，测试进程的 RotatingFileHandler 轮转改名
    会报 WinError 32（文件被占用）。caplog 不依赖 FileHandler，去掉不影响日志断言。"""
    return os.environ.get("PYTEST_CURRENT_TEST") is not None or "pytest" in sys.modules


def setup_logging():
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 幂等：uvicorn reload / lifespan 重复进入时不重复挂 handler。
    if not any(getattr(h, "_core_logging", False) for h in root.handlers):
        if not _under_pytest():
            log_dir = Path(__file__).resolve().parent.parent.parent / "logs"
            log_dir.mkdir(exist_ok=True)
            log_file = log_dir / "core.log"
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            file_handler.setFormatter(formatter)
            file_handler._core_logging = True
            root.addHandler(file_handler)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler._core_logging = True
        root.addHandler(console_handler)

    # 压低第三方 HTTP 客户端噪音：httpx/httpcore 默认每拍一行 "HTTP Request ... 200/404"，
    # 会快速轮转冲掉盘中关键日志（14:30 日终、13:47 熔断实时日志因此丢失）。只留 WARNING+。
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
