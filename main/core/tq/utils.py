import threading
from dataclasses import dataclass
from typing import List


class TDXConnectionError(Exception):
    pass


@dataclass
class TQConfig:
    backtest_path: str = ""
    live_path: str = ""
    mode: str = "backtest"


_tdx_lock = threading.Lock()


def get_tdx_lock():
    return _tdx_lock
