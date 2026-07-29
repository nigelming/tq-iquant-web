from typing import Callable
from .portfolio import Portfolio


class BacktestEngine:
    def run(
        self,
        portfolio: Portfolio,
        klines: dict = None,
        signal_cache: dict = None,
        benchmark_data: object = None,
        progress_callback: Callable = None,
    ) -> dict:
        trades = []
        snapshots = []
        total = 100
        for i in range(total):
            if progress_callback:
                progress_callback(i + 1)
        result = {
            "trades": trades,
            "snapshots": snapshots,
        }
        return result
