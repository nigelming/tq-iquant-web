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


def _import_tq():
    """导入 tqcenter.tq；失败统一转成 TDXConnectionError（而非裸 ModuleNotFoundError）。

    裸 ModuleNotFoundError 不会被路由层识别，会冒泡成"服务器内部错误"，
    用户无法知道是通达信路径配置问题。
    """
    try:
        from tqcenter import tq
    except ModuleNotFoundError as e:
        raise TDXConnectionError(
            "无法加载 tqcenter（未在通达信目录的 PYPlugins 下找到 tqcenter.py）。"
            f"请检查配置中的通达信路径是否正确，当前路径：{_tdx_path()}"
        ) from e
    return tq


def get_tq():
    """获取 tqcenter.tq 单例，首次调用惰性 initialize 连接。

    连接标识用本文件路径（utils.py），唯一标识本进程。
    需通达信（回测版 new_tdx64）已启动并登录行情。
    """
    global _tq, _tq_initialized
    if _tq is None:
        inject_tqcenter_path(_tdx_path())
        _tq = _import_tq()
    if not _tq_initialized:
        try:
            _tq.initialize(__file__)
        except TDXConnectionError:
            raise
        except Exception as e:
            raise TDXConnectionError(f"连接通达信失败，请确认通达信已启动并登录：{e}") from e
        _tq_initialized = True
    return _tq


def init_tq(tdx_path: str):
    """显式初始化（兼容旧接口）；tdx_path 用于注入 sys.path。"""
    global _tq, _tq_initialized
    inject_tqcenter_path(tdx_path)
    if _tq is None:
        _tq = _import_tq()
    try:
        _tq.initialize(__file__)
    except TDXConnectionError:
        raise
    except Exception as e:
        raise TDXConnectionError(f"连接通达信失败，请确认通达信已启动并登录：{e}") from e
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
