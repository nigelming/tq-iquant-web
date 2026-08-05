from typing import Dict, List, Optional
from .utils import get_tdx_lock, get_tq


class TQFormula:
    def compute(
        self, formula_name: str, formula_arg: str,
        stocks: List[str], period: str = "1d",
        count: int = 10, dividend_type: int = 1,
        start_time: str = "", end_time: str = "",
        return_count: int = -1, return_date: bool = True,
    ) -> Optional[dict]:
        with get_tdx_lock():
            return self._run_formula(
                formula_name, formula_arg, stocks, period,
                count, dividend_type, start_time, end_time,
                return_count, return_date,
            )

    def compute_xg(
        self, formula_name: str, formula_arg: str,
        stocks: Optional[List[str]] = None,
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
    ) -> Optional[dict]:
        with get_tdx_lock():
            return self._run_xg(formula_name, formula_arg, stocks, period, start_time, end_time)

    def compute_injected(
        self, formula_name: str, ohlcv_df: dict,
        stocks: List[str], period: str = "1m",
        dividend_type: int = 1, formula_arg: str = "",
    ) -> Optional[dict]:
        """内存注入算公式（实盘逐 bar 链路，0010）。

        ohlcv_df: {Amount/Volume/Close/Open/High/Low: pandas.DataFrame}（列=股票代码，
            行=DatetimeIndex），由 _bars_to_formula_df 从桥 bar 构造。
        链路：formula_format_data → 逐股票 formula_set_data(dividend_type=0 内存注入)
            → formula_process_mul_zb(count=-1，不传时间范围，让公式用注入的全部数据)。
        任一股票 set_data 失败（ErrorId != 0）→ 返回 None。
        get_tdx_lock() 串行化，与回测互不并发（同进程共用 get_tq() 单例）。
        返回 formula_process_mul_zb 的 raw（同 compute）。
        """
        with get_tdx_lock():
            tq = get_tq()
            formatted = tq.formula_format_data(ohlcv_df)
            if not formatted:
                return None
            for code in stocks:
                stock_data = formatted.get(code)
                if stock_data is None or len(stock_data) == 0:
                    return None
                sd = tq.formula_set_data(
                    stock_code=code, stock_period=period,
                    stock_data=stock_data, count=len(stock_data),
                    dividend_type=0,
                )
                if not sd or str(sd.get("ErrorId", "1")) != "0":
                    return None
            return tq.formula_process_mul_zb(
                formula_name=formula_name,
                formula_arg=formula_arg,
                return_count=-1,
                return_date=True,
                xsflag=-1,
                stock_list=stocks,
                stock_period=period,
                start_time="",
                end_time="",
                count=-1,
                dividend_type=dividend_type,
            )

    def get_formula_list(self, formula_type: int = 0) -> List[dict]:
        tq = get_tq()
        return tq.formula_get_all(formula_type=formula_type) or []

    def _run_formula(self, formula_name, formula_arg, stocks, period, count, dividend_type,
                     start_time="", end_time="", return_count=-1, return_date=True):
        tq = get_tq()
        return tq.formula_process_mul_zb(
            formula_name=formula_name,
            formula_arg=formula_arg,
            return_count=return_count,
            return_date=return_date,
            xsflag=-1,
            stock_list=stocks,
            stock_period=period,
            start_time=start_time,
            end_time=end_time,
            count=count,
            dividend_type=dividend_type,
        )

    def _run_xg(self, formula_name, formula_arg, stocks, period, start_time, end_time):
        tq = get_tq()
        return tq.formula_process_mul_xg(
            formula_name=formula_name,
            formula_arg=formula_arg,
            stock_list=stocks or [],
            stock_period=period,
            start_time=start_time,
            end_time=end_time,
        )
