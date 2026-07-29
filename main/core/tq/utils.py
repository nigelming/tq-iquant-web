import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


class TDXConnectionError(Exception):
    pass


@dataclass
class TQConfig:
    backtest_path: str = ""
    live_path: str = ""
    mode: str = "backtest"


_tdx_lock = threading.Lock()
_tq = None


def get_tdx_lock():
    return _tdx_lock


def inject_tqcenter_path(tdx_path: str) -> bool:
    p = Path(tdx_path) / "PYPlugins"
    for sub in ("sys", "user"):
        candidate = p / sub
        if candidate.exists():
            s = str(candidate)
            if s not in sys.path:
                sys.path.append(s)
    return True


def get_tq():
    global _tq
    if _tq is None:
        inject_tqcenter_path("D:\\new_tdx64")
        from tqcenter import tq
        _tq = tq
    return _tq


def init_tq(tdx_path: str):
    global _tq
    tq_mod = get_tq()
    tq_mod.initialize(__file__)
    _tq = tq_mod


def close_tq():
    global _tq
    if _tq is not None:
        try:
            _tq.close()
        except Exception:
            pass
        _tq = None
