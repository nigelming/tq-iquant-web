"""跨引擎 UI 自动化的弹窗归属识别与互斥（tdx/ths 双引擎并行，2026-11-04）。

两个交易引擎（通达信 TDX / 同花顺 THS）同时运行时，各自的「下单确认/验证码/
提示」弹窗都浮在桌面上。此前各 dispatcher 按桌面全局枚举找弹窗（只过滤
「新增 + 按钮关键词」）——TDX 的确认自动化可能点到 THS 的弹窗（或反向），
鼠标/键盘模拟还会互相抢焦点。

本模块提供两件套：
- ``window_image_name(pid)``：进程映像名（ctypes QueryFullProcessImageName，
  带缓存），供弹窗归属判定；
- ``UI_AUTO_LOCK``：进程级互斥锁——任何鼠标/键盘模拟段落必须持锁执行，
  双引擎的 UI 自动化在此串行（同进程内不同线程）。

通道判定：弹窗属主进程映像名含 "tdx" → 通达信；含 "xiadan"/"hexin" → 同花顺。
"""
from __future__ import annotations

import ctypes
import logging
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# 双引擎 UI 自动化互斥（进程级）：THS _execute_via_ui / TDX _confirm_order_dialog
# 等鼠标键盘段落持锁串行，防焦点/鼠标竞态。
UI_AUTO_LOCK = threading.RLock()

# 通道 → 属主进程映像名关键词（小写）
_CHANNEL_PROC_KEYWORDS = {
    "tdx": ("tdx",),
    "ths": ("xiadan", "hexin"),
}

_pid_image_cache: dict = {}
_cache_lock = threading.Lock()


def window_image_name(pid: int) -> str:
    """进程映像文件名（小写 basename），失败返回空串。带缓存。"""
    if not pid:
        return ""
    with _cache_lock:
        cached = _pid_image_cache.get(pid)
    if cached is not None:
        return cached
    name = ""
    try:
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if h:
            try:
                size = 512
                buf = ctypes.create_unicode_buffer(size)
                n = ctypes.c_ulong(size)
                # QueryFullProcessImageNameW（Vista+，不受权限模型限制）
                if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                    full = buf.value
                    name = full.rsplit("\\", 1)[-1].lower()
            finally:
                k32.CloseHandle(h)
    except Exception:  # noqa: BLE001
        name = ""
    with _cache_lock:
        _pid_image_cache[pid] = name
    return name


def dialog_matches_channel(dialog, channel: str) -> bool:
    """弹窗是否属于指定通道的客户端进程（映像名关键词匹配）。

    channel 不在已知映射（如 iquant 桥不弹本机窗口）→ 放行 True（不做过滤，
    由调用方原语义）。取 PID 失败（映像名空）→ 放行 True（宁宽勿错，与
    交易日历 fail-open 同则）——避免安全软件干扰导致自动化全部哑火。
    """
    keywords = _CHANNEL_PROC_KEYWORDS.get(channel)
    if not keywords:
        return True
    try:
        pid = dialog.process_id()
    except Exception:  # noqa: BLE001
        return True
    image = window_image_name(pid)
    if not image:
        return True
    return any(k in image for k in keywords)
