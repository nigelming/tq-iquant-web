"""config._defaults() 关键默认值回归（ declarative，防误改）。"""
from core.config import _defaults


def test_live_alert_send_warn_default_disabled():
    """hermes 日频信号（0017）：send_warn 默认关，config.yaml 可设 true 恢复。"""
    assert _defaults()["live"]["alert_send_warn_enabled"] is False
