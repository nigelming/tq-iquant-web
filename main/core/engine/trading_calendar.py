"""向后兼容 shim：TradingCalendar 已迁入 ``core.engine.live.calendar``（0010 后续）。

本模块仅 re-export，保留 ``from core.engine.trading_calendar import TradingCalendar``
这一既有 import 路径（live_engine、测试及外部调用方零改动）。新代码请直接从
``core.engine.live.calendar`` 导入。
"""
from __future__ import annotations

from .live.calendar import (
    CalendarProvider,
    TradingCalendar,
    _MARKET_CLOSE,
    _MARKET_OPEN,
)

__all__ = [
    "TradingCalendar",
    "CalendarProvider",
    "_MARKET_OPEN",
    "_MARKET_CLOSE",
]
