import polars as pl
from typing import Dict, List
from .utils import get_tdx_lock


class TQFormula:
    def compute(
        self, formula_text: str, stocks: List[str], period: str,
    ) -> Dict[str, pl.DataFrame]:
        with get_tdx_lock():
            return self._run_formula(formula_text, stocks, period)

    def _run_formula(self, formula_text, stocks, period):
        raise NotImplementedError("TDX not connected")
