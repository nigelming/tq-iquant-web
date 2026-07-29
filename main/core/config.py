import os
from pathlib import Path
from typing import Optional

import yaml


_default_config_path = Path(__file__).resolve().parent.parent.parent / "config.yaml"


def get_config_path() -> Path:
    return _default_config_path


def load_config(config_path: Optional[str] = None) -> dict:
    path = Path(config_path) if config_path else _default_config_path
    if not path.exists():
        return _defaults()
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    cfg.setdefault("database", {}).setdefault("password", os.environ.get("TQ_DB_PASSWORD", ""))
    merged = _defaults().copy()
    merged.update(cfg)
    return merged


def save_config(cfg: dict, config_path: Optional[str] = None) -> None:
    path = Path(config_path) if config_path else _default_config_path
    safe = {k: v for k, v in cfg.items() if k != "database"}
    safe["database"] = {k: v for k, v in cfg.get("database", {}).items() if k != "password"}
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(safe, f, default_flow_style=False, allow_unicode=True)


def _defaults() -> dict:
    return {
        "tdx_backtest_path": "",
        "tdx_live_path": "",
        "iquant_path": "",
        "max_concurrent_backtest": 1,
        "database": {
            "host": "localhost",
            "port": 5432,
            "database": "tq_iquant",
            "user": "postgres",
            "password": os.environ.get("TQ_DB_PASSWORD", ""),
        },
        "nats": {
            "url": "nats://localhost:4222",
        },
    }
