import sys
import threading
from pathlib import Path
from typing import Optional

from core.config import load_config


class TDXConnectionError(Exception):
    pass


_tdx_lock = threading.Lock()
_tq = None
_tq_initialized = False


def get_tdx_lock():
    return _tdx_lock


def _tdx_path() -> str:
    """从 config.yaml 读 tdx_path；缺失回退默认。"""
    cfg = load_config()
    p = cfg.get("tdx_path", "") or "D:\\new_tdx64"
    return p


def inject_tqcenter_path(tdx_path: str) -> bool:
    p = Path(tdx_path) / "PYPlugins"
    for sub in ("sys", "user"):
        candidate = p / sub
        if candidate.exists():
            s = str(candidate)
            if s not in sys.path:
                sys.path.append(s)
    return True


def get_tq():
    """获取 tqcenter.tq 单例，首次调用惰性 initialize 连接。

    连接标识用本文件路径（utils.py），唯一标识本进程。
    需通达信（回测版 new_tdx64）已启动并登录行情。
    """
    global _tq, _tq_initialized
    if _tq is None:
        inject_tqcenter_path(_tdx_path())
        from tqcenter import tq
        _tq = tq
    if not _tq_initialized:
        _tq.initialize(__file__)
        _tq_initialized = True
    return _tq


def init_tq(tdx_path: str):
    """显式初始化（兼容旧接口）；tdx_path 用于注入 sys.path。"""
    global _tq, _tq_initialized
    inject_tqcenter_path(tdx_path)
    if _tq is None:
        from tqcenter import tq
        _tq = tq
    _tq.initialize(__file__)
    _tq_initialized = True


def close_tq():
    global _tq, _tq_initialized
    if _tq is not None:
        try:
            _tq.close()
        except Exception:
            pass
        _tq = None
        _tq_initialized = False
