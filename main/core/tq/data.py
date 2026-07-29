import polars as pl
from typing import Dict, List, Callable, Optional
from .utils import get_tdx_lock


class TQData:
    def get_stock_pools(self) -> List[dict]:
        with get_tdx_lock():
            return self._get_pools()

    def get_pool_stocks(self, pool_name: str) -> List[dict]:
        with get_tdx_lock():
            return self._get_stocks(pool_name)

    def get_history(
        self, stocks: List[str], periods: List[str],
        start: str, end: str,
    ) -> Dict[str, Dict[str, pl.DataFrame]]:
        with get_tdx_lock():
            return self._load_history(stocks, periods, start, end)

    def subscribe_bars(
        self, stocks: List[str], periods: List[str],
        callback: Callable,
    ) -> None:
        with get_tdx_lock():
            self._subscribe(stocks, periods, callback)

    # --- TDX 底层调用（待接入真实通达信） ---
    def _get_pools(self) -> List[dict]:
        raise NotImplementedError("TDX not connected")

    def _get_stocks(self, pool_name: str) -> List[dict]:
        raise NotImplementedError("TDX not connected")

    def _load_history(self, stocks, periods, start, end):
        raise NotImplementedError("TDX not connected")

    def _subscribe(self, stocks, periods, callback):
        raise NotImplementedError("TDX not connected")
