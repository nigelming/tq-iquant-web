import polars as pl
from typing import Dict, List, Callable, Optional
from .utils import get_tdx_lock, get_tq


class TQData:
    def get_stock_pools(self) -> List[dict]:
        with get_tdx_lock():
            return self._get_pools()

    def get_pool_stocks(self, pool_name: str) -> List[dict]:
        with get_tdx_lock():
            return self._get_stocks(pool_name)

    def get_history(
        self, stocks: List[str], periods: List[str],
        start: str = "", end: str = "",
        dividend_type: str = "front", count: int = 100,
    ) -> Dict[str, Dict[str, pl.DataFrame]]:
        with get_tdx_lock():
            return self._load_history(stocks, periods, start, end, dividend_type, count)

    def subscribe_bars(
        self, stocks: List[str], periods: List[str],
        callback: Callable,
    ) -> None:
        with get_tdx_lock():
            self._subscribe(stocks, periods, callback)

    def get_all_stocks(self, market: str = "5") -> List[str]:
        tq = get_tq()
        return tq.get_stock_list(market) or []

    def get_sectors(self, list_type: int = 1) -> List[dict]:
        tq = get_tq()
        return tq.get_sector_list(list_type=list_type) or []

    def get_stocks_in_sector(self, block_code: str) -> List[str]:
        tq = get_tq()
        return tq.get_stock_list_in_sector(block_code) or []

    # --- TDX 底层调用 ---
    def _get_pools(self) -> List[dict]:
        tq = get_tq()
        sectors = tq.get_sector_list(list_type=1) or []
        return [{"name": s} for s in sectors]

    def _get_stocks(self, pool_name: str) -> List[dict]:
        tq = get_tq()
        stocks = tq.get_stock_list_in_sector(pool_name) or []
        return [{"stock_code": s} for s in stocks]

    def _load_history(
        self, stocks, periods, start="", end="",
        dividend_type="front", count=100,
    ) -> Dict[str, Dict[str, pl.DataFrame]]:
        tq = get_tq()
        result = {}
        for period in periods:
            df = tq.get_market_data(
                stock_list=stocks,
                count=count,
                period=period,
                dividend_type=dividend_type,
            )
            if df is not None:
                for code in stocks:
                    if code not in result:
                        result[code] = {}
                    try:
                        series = {}
                        for col in ["Open", "High", "Low", "Close", "Volume", "Amount"]:
                            if col in df and code in df[col].columns:
                                series[col] = df[col][code]
                        if series:
                            result[code][period] = pl.DataFrame(series)
                    except Exception:
                        pass
        return result

    def _subscribe(self, stocks, periods, callback):
        tq = get_tq()
        tq.subscribe_hq(stock_list=stocks, callback=callback)
