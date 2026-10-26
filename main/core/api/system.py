from fastapi import APIRouter

from core.api.response import ok
from core.services.system_service import get_config, update_config
from core.services import auto_shutdown

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/configs")
def get_config_route():
    return ok(get_config())


@router.put("/configs")
def update_config_route(data: dict):
    update_config(data)
    return ok()


@router.api_route("/heartbeat", methods=["GET", "POST"])
def heartbeat_route():
    """前端标签页存活心跳（打包版自动退出用；页面打开时每 5s 发送一次）。

    前端经 navigator.sendBeacon 发送（固定为 POST），GET 保留供直接调用/测试。
    """
    auto_shutdown.heartbeat()
    return ok()


@router.post("/closing")
def closing_route():
    """前端标签页关闭信号（pagehide）。打包版：宽限期内无新心跳则退出进程。"""
    auto_shutdown.request_close()
    return ok()


@router.post("/shutdown")
def shutdown_route():
    """显式退出：打包版退出进程并释放端口；开发模式 no-op。"""
    auto_shutdown.shutdown_now()
    return ok()
