from typing import Dict, List, Optional
from .utils import get_tdx_lock, get_tq


class TQFormula:
    def compute(
        self, formula_name: str, formula_arg: str,
        stocks: List[str], period: str = "1d",
        count: int = 10, dividend_type: int = 1,
    ) -> Optional[dict]:
        with get_tdx_lock():
            return self._run_formula(formula_name, formula_arg, stocks, period, count, dividend_type)

    def compute_xg(
        self, formula_name: str, formula_arg: str,
        stocks: Optional[List[str]] = None,
        period: str = "1d",
        start_time: str = "",
        end_time: str = "",
    ) -> Optional[dict]:
        with get_tdx_lock():
            return self._run_xg(formula_name, formula_arg, stocks, period, start_time, end_time)

    def get_formula_list(self, formula_type: int = 0) -> List[dict]:
        tq = get_tq()
        return tq.formula_get_all(formula_type=formula_type) or []

    def _run_formula(self, formula_name, formula_arg, stocks, period, count, dividend_type):
        tq = get_tq()
        return tq.formula_process_mul_zb(
            formula_name=formula_name,
            formula_arg=formula_arg,
            stock_list=stocks,
            stock_period=period,
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
