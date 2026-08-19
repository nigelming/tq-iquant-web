"""系统配置 Service 层（P1 #9 续块）。

承接 core.api.system 的业务逻辑：load_config / save_config 包装。
路由层仅剩 HTTP 入口 + ok 包装（无 404/400/409，无 DB）。
"""
from core.config import load_config, save_config


def get_config() -> dict:
    return load_config()


def update_config(data: dict) -> None:
    save_config(data)
