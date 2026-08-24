"""交易日历与交易总开关。

权威交易日由桥 ``xtdata.get_trading_dates`` 提供（GET /calendar），Core 按自然年
缓存一次。判定原则：

- 周末（周六/周日）永远非交易日——A 股硬规则，不依赖数据，也不 fail-open。
- 工作日是否交易日由桥返回的权威集合判定（覆盖节假日/调休）。
- 桥离线/返回空（老桥无 /calendar、网络抖动）时 **fail-open**：工作日默认交易日。
  宁可节假日误放行（下单会被柜台拒并由 _expire/清扫兜底），也绝不在真实交易日因
  日历过期而拦下所有下单。仅记录一次告警，不刷屏。
- ``is_trading_allowed(now)`` = 交易日 且 09:30 ≤ 时刻 < 15:00，作为下单总闸。

注意：这里只负责"是否允许下单"。引擎生命周期（start/stop）、14:30 估值、15:05 收盘
清扫各有自己的调度判定，不被本开关替代。
"""
from __future__ import annotations

import logging
from datetime import date, datetime, time
from typing import Callable, Iterable, Optional, Set

logger = logging.getLogger(__name__)

# A 股连续竞价时段（下单总闸用，与 live_engine 的收盘守卫一致）
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(15, 0)

# provider 返回的交易日列表：元素为 date 或 'YYYYMMDD' 字符串
CalendarProvider = Callable[[], Iterable]


class TradingCalendar:
    def __init__(self, provider: CalendarProvider):
        self._provider = provider
        # year -> set[date]；None 表示该年尝试拉取失败（fail-open），不再重试
        self._by_year: dict = {}
        self._warned_year: Set[int] = set()

    def _load_year(self, year: int) -> Optional[Set[date]]:
        """返回某自然年的交易日集合；None 表示无数据（fail-open）。"""
        if year in self._by_year:
            return self._by_year[year]
        dates: Set[date] = set()
        try:
            raw = list(self._provider() or [])
        except Exception as exc:  # 桥离线/超时/老桥 404
            raw = []
            if year not in self._warned_year:
                logger.warning(
                    "trading calendar unavailable (%s); fail-open: treating weekdays "
                    "as trading days for %s", exc, year)
                self._warned_year.add(year)
        for item in raw:
            d = self._coerce(item)
            if d is not None:
                dates.add(d)
        # 空集合存为 None，触发 fail-open；非空才当权威数据
        self._by_year[year] = dates if dates else None
        if not dates and year not in self._warned_year:
            logger.warning(
                "trading calendar empty for %s; fail-open: treating weekdays as "
                "trading days", year)
            self._warned_year.add(year)
        return self._by_year[year]

    @staticmethod
    def _coerce(item) -> Optional[date]:
        if isinstance(item, date):
            return item
        if isinstance(item, datetime):
            return item.date()
        # 桥把毫秒时间戳转成 'YYYYMMDD' 字符串
        text = str(item).strip()
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        return None

    def is_trading_day(self, d: date) -> bool:
        # 周末硬规则（weekday(): Monday=0 .. Sunday=6）
        if d.weekday() >= 5:
            return False
        trading = self._load_year(d.year)
        if trading is None:
            return True  # fail-open：工作日无日历数据，放行
        return d in trading

    def is_trading_allowed(self, now: datetime) -> bool:
        """是否允许在 ``now`` 时刻下单：交易日 且 09:30–15:00（左闭右开）。"""
        if not self.is_trading_day(now.date()):
            return False
        t = now.time()
        return _MARKET_OPEN <= t < _MARKET_CLOSE
