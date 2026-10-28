"""市场交易规则（回测/实盘共用）。

涨跌停幅度口径回测与实盘必须一致：回测 Portfolio 的涨跌停延后判定与实盘
TdxTqDispatcher 的下单前置校验共用同一函数，避免两处口径漂移。

组合交易时段（trading_session）闸同属两端口径：回测按 bar 时间、实盘按
墙钟判定，允许窗口在此统一定义（market_rules 与涨跌停幅度同级的共享规则）。
"""
from datetime import time as dtime
from decimal import Decimal
from typing import Optional


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


# trading_session（组合表字段，full/am/pm）允许时段（上海时间，含端点）：
# am=09:30–11:30（早盘）、pm=13:00–15:00（午盘）、full=不筛（默认）。
TRADING_SESSION_WINDOWS = {
    "am": (dtime(9, 30), dtime(11, 30)),
    "pm": (dtime(13, 0), dtime(15, 0)),
}


def session_allows(session: Optional[str], t: dtime) -> bool:
    """组合交易时段闸：session 非 am/pm（含 None/full/未知值）一律放行。

    回测传 bar_time.time()、实盘传墙钟 now_shanghai().time()，判定同口径。
    注意日线级 bar 的 bar_time 按约定是 00:00——am/pm 组合在回测中会跳过
    全部日线 bar（实盘日线由 14:30 closer 驱动，pm 组合仍可处理）。
    """
    window = TRADING_SESSION_WINDOWS.get(session or "")
    if window is None:
        return True
    return window[0] <= t <= window[1]
