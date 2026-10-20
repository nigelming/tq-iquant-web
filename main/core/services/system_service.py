"""系统配置 Service 层（P1 #9 续块）。

承接 core.api.system 的业务逻辑：load_config / save_config 包装。
路由层仅剩 HTTP 入口 + ok 包装（无 404/400/409，无 DB）。
"""
from core.config import load_config, save_config, _deep_merge


def get_config() -> dict:
    return load_config()


def update_config(data: dict) -> None:
    """全量覆盖写，但先与现有配置深合并——避免前端固定 5 键重建时丢掉
    未在表单中暴露的键（如手工加的配置项）。"""
    save_config(_deep_merge(load_config(), data))
