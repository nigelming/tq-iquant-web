import polars as pl
from typing import Dict, List, Callable, Optional
from .utils import get_tdx_lock, get_tq


class TQData:
    def get_stock_pools(self) -> List[dict]:
        with get_tdx_lock():
            return self._get_pools()

    def get_pool_stocks(self, pool_code: str) -> List[dict]:
        with get_tdx_lock():
            return self._get_stocks(pool_code)

    def get_history(
        self, stocks: List[str], periods: List[str],
        start: str = "", end: str = "",
        dividend_type: str = "front", count: int = 100,
    ) -> Dict[str, Dict[str, pl.DataFrame]]:
        with get_tdx_lock():
            return self._load_history(stocks, periods, start, end, dividend_type, count)

    def get_history_raw(
        self, stocks: List[str], periods: List[str],
        start: str = "", end: str = "",
        dividend_type: str = "front", count: int = 100,
    ) -> Dict[str, dict]:
        """取原始 TQ 行情（未经转换），按周期分组：{period: {field: pandas.DataFrame}}。

        field ∈ Open/High/Low/Close/Volume/Amount，DataFrame.index=时间戳，columns=股票代码。
        一次调用 = 单周期多股票。转换由调用方（_convert_market_data）负责，便于单测。
        """
        with get_tdx_lock():
            return self._load_history_raw(stocks, periods, start, end, dividend_type, count)

    def subscribe_bars(
        self, stocks: List[str], periods: List[str],
        callback: Callable,
    ) -> None:
        with get_tdx_lock():
            self._subscribe(stocks, periods, callback)

    def get_all_stocks(self, market: str = "5") -> List[str]:
        tq = get_tq()
        return tq.get_stock_list(market) or []

    # --- TDX 底层调用 ---
    def _get_pools(self) -> List[dict]:
        """通达信用户自定义板块，归一化为 [{"code","name"}]。

        SDK get_user_sector() 返回 [{"Code","Name"}]（大写首字母）。
        v1 曾用 get_sector_list（系统板块，587 个，非用户板块）且把 dict 当 str → 线上炸。
        """
        tq = get_tq()
        sectors = tq.get_user_sector() or []
        return [{"code": s["Code"], "name": s["Name"]} for s in sectors]

    def _get_stocks(self, pool_code: str) -> List[dict]:
        """板块成分股，归一化为 [{"stock_code","stock_name"}]。

        SDK get_stock_list_in_sector(code, block_type=1, list_type=1) 返回
        [{"Code","Name"}]。必须传板块 Code（如 TQCS），传 Name 返回空。
        """
        tq = get_tq()
        stocks = tq.get_stock_list_in_sector(pool_code, block_type=1, list_type=1) or []
        return [
            {"stock_code": s["Code"], "stock_name": s["Name"]}
            for s in stocks
        ]

    def _load_history(
        self, stocks, periods, start="", end="",
        dividend_type="front", count=100,
    ) -> Dict[str, Dict[str, pl.DataFrame]]:
        tq = get_tq()
        result = {}
        for period in periods:
            df = tq.get_market_data(
                field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
                stock_list=stocks,
                period=period,
                start_time=start,
                end_time=end,
                count=count,
                dividend_type=dividend_type,
                fill_data=True,
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

    def _load_history_raw(
        self, stocks, periods, start="", end="",
        dividend_type="front", count=100,
    ) -> Dict[str, dict]:
        """原始 TQ 行情按周期分组：{period: raw_dict}。raw_dict 即 tq.get_market_data 返回值。"""
        tq = get_tq()
        result: Dict[str, dict] = {}
        for period in periods:
            df = tq.get_market_data(
                field_list=["Open", "High", "Low", "Close", "Volume", "Amount"],
                stock_list=stocks,
                period=period,
                start_time=start,
                end_time=end,
                count=count,
                dividend_type=dividend_type,
                fill_data=True,
            )
            if df is not None:
                result[period] = df
        return result

    def _subscribe(self, stocks, periods, callback):
        tq = get_tq()
        tq.subscribe_hq(stock_list=stocks, callback=callback)
