from pathlib import Path
from typing import Optional

import yaml


_default_config_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"


def get_config_path() -> Path:
    return _default_config_path


def _defaults() -> dict:
    return {
        "tdx_path": "",
        "iquant_path": "",
        "max_concurrent_backtest": 1,
        "database": {
            "sqlite_path": "data/dev.db",
        },
        "iquant_bridge": {
            "simulation": {"base_url": "http://127.0.0.1:8790"},
            "live": {"base_url": "http://127.0.0.1:8791"},
        },
    }


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并：override 覆盖 base，嵌套 dict 逐层合并而非整体替换。"""
    merged = base.copy()
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_config(config_path: Optional[str] = None) -> dict:
    path = Path(config_path) if config_path else _default_config_path
    if not path.exists():
        return _defaults()
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return _deep_merge(_defaults(), cfg)


def save_config(cfg: dict, config_path: Optional[str] = None) -> None:
    path = Path(config_path) if config_path else _default_config_path
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
