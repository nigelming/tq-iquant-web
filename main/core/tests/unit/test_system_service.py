"""system_service.update_config 合并写入单测（防盲覆盖丢配置）。"""
from unittest.mock import patch

from core.services import system_service


def test_update_config_deep_merges_preserving_unmentioned_keys():
    base = {
        "tdx_path": "D:/tdx",
        "max_concurrent_backtest": 1,
        "iquant_bridge": {
            "simulation": {"base_url": "http://127.0.0.1:8790"},
            "live": {"base_url": "http://127.0.0.1:8791"},
        },
    }
    captured = {}

    with patch.object(system_service, "load_config", return_value=base), \
         patch.object(system_service, "save_config", side_effect=lambda cfg: captured.update(cfg)):
        system_service.update_config({
            "max_concurrent_backtest": 2,
            "iquant_bridge": {"live": {"base_url": "http://127.0.0.1:9999"}},
        })

    assert captured["tdx_path"] == "D:/tdx"
    assert captured["max_concurrent_backtest"] == 2
    assert captured["iquant_bridge"]["simulation"]["base_url"] == "http://127.0.0.1:8790"
    assert captured["iquant_bridge"]["live"]["base_url"] == "http://127.0.0.1:9999"
