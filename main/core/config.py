import os
import sys
from pathlib import Path
from typing import Optional

import yaml


def is_frozen_app() -> bool:
    """是否为打包后的独立程序（exe）。

    Nuitka 与 PyInstaller 不同：standalone 编译后**不会**设置 sys.frozen，
    必须额外用 argv[0] 是否为 .exe 判断；否则所有"包内资源定位"都会错误
    回退到 __file__ 推算路径（实测导致读到空配置、tdx_path 回退错误目录）。

    pytest 下 argv[0] 可能是 pytest.exe，需显式排除，避免测试误判为打包环境。
    """
    if os.environ.get("PYTEST_CURRENT_TEST") is not None or "pytest" in sys.modules:
        return False
    return hasattr(sys, "frozen") or Path(sys.argv[0]).suffix.lower() == ".exe"


def _default_config_path() -> Path:
    if is_frozen_app():
        return Path(sys.argv[0]).resolve().parent / "config.yaml"
    return Path(__file__).resolve().parent.parent.parent / "config.yaml"


def get_config_path() -> Path:
    return _default_config_path()


def _defaults() -> dict:
    return {
        "tdx_path": "D:\\zd_zyb",
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
    path = Path(config_path) if config_path else _default_config_path()
    if not path.exists():
        return _defaults()
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return _deep_merge(_defaults(), cfg)


def save_config(cfg: dict, config_path: Optional[str] = None) -> None:
    path = Path(config_path) if config_path else _default_config_path()
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)
