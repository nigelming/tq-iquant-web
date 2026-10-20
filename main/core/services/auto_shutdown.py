"""打包版（frozen）自动退出：浏览器标签页全部关闭后自动退出进程，避免残留进程占用端口。

机制（仅 frozen 打包 exe 启用，开发 uvicorn 不启用）：
- 前端每个标签页每 5s 发心跳 POST /api/system/heartbeat（sendBeacon，页面打开期间保持服务存活）。
- 标签页关闭（pagehide）发 POST /api/system/closing：若 _CLOSE_GRACE 秒内无任何
  新心跳（=没有其他活跃标签页），判定页面已全部关闭 → 进程退出。
- 显式退出按钮发 POST /api/system/shutdown：0.5s 后退出进程。
"""
import os
import threading
import time

_armed = False
_lock = threading.Lock()
_last_heartbeat = 0.0
_pending_close = False

_CLOSE_GRACE = 8.0  # 关闭信号后的宽限期（秒），覆盖刷新/新标签页接入的间隙


def arm() -> None:
    """打包 exe 启动时调用：启用自动退出。开发模式不调用 → 全部接口 no-op。"""
    global _armed
    _armed = True


def is_armed() -> bool:
    return _armed


def heartbeat() -> None:
    global _last_heartbeat, _pending_close
    with _lock:
        _last_heartbeat = time.monotonic()
        _pending_close = False


def request_close() -> None:
    """浏览器标签页关闭信号（pagehide）。宽限期内有新心跳则取消退出。"""
    global _pending_close
    with _lock:
        _pending_close = True
        last = _last_heartbeat
    if not _armed:
        return

    def _check() -> None:
        time.sleep(_CLOSE_GRACE)
        with _lock:
            if _armed and _pending_close and time.monotonic() - last >= _CLOSE_GRACE:
                os._exit(0)

    threading.Thread(target=_check, daemon=True).start()


def shutdown_now() -> None:
    """显式退出：给 0.5s 让响应 flush，然后退出进程。未启用（开发模式）则 no-op。"""
    if not _armed:
        return

    def _exit() -> None:
        time.sleep(0.5)
        os._exit(0)

    threading.Thread(target=_exit, daemon=True).start()
