"""市场交易规则（回测/实盘共用）。

涨跌停幅度口径回测与实盘必须一致：回测 Portfolio 的涨跌停延后判定与实盘
TdxTqDispatcher 的下单前置校验共用同一函数，避免两处口径漂移。
"""
from decimal import Decimal


def _limit_ratio_for(stock_code: str) -> Decimal:
    """A 股涨跌停幅度：创业板(300/301)/科创板(688) 20%，北交所(4/8 前缀) 30%，其余 10%。

    ST 5% 不特判（v1 偏宽松可接受：对 ST 股拦截区间偏宽，少拦不错拦）。
    """
    prefix = stock_code.split(".")[0]
    if prefix.startswith(("300", "301", "688")):
        return Decimal("0.20")
    if prefix[:1] in ("4", "8"):
        return Decimal("0.30")
    return Decimal("0.10")
