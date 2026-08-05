"""BarPoller — 实盘行情通道（0009 切片3）。

定时拉桥 GET /quote → bar 完成检测（stime <= now）→ 只触发 > last_bar_time 的已完成 bar
→ 合并多股票为一根 BarEvent → 驱动 Portfolio.on_bar。

桥 /quote 返回（live/bridge/iquant_bridge.py get_quote）：
  {"ok": True, "data": {code: [bar, ...]}}
每 bar = DataFrame reset_index().to_dict("records")，字段含
stime（yyyymmddHHMMSS 字符串，bar **结束**时间）/ open / high / low / close / volume。

bar 完成检测（0009 §5.2 验证语义）：
  - stime 是 bar 结束时间（如 10:08:00 = 10:07–10:08 那根结束）
  - stime <= now → 已完成，可触发信号
  - stime > now  → 进行中（OHLC 还在变），忽略，防信号闪烁/未来函数
  - > last_bar_time → 新 bar，触发；<= last_bar_time → 旧 bar，不重复触发

5m 是原生周期直接拉（0009 §5.1 验证定案，不做 1m→5m 聚合）。
"""
from datetime import datetime
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from .event import BarEvent
from .http_bridge_dispatcher import HttpBridgeDispatcher


def parse_bar_time(stime) -> Optional[datetime]:
    """桥 bar 的 stime（yyyymmddHHMMSS 字符串）→ datetime。

    stime 是 bar **结束**时间。非法/空 → None。
    """
    if not stime:
        return None
    s = str(stime).strip()
    # yyyymmddHHMMSS（14 位数字）
    if len(s) >= 14 and s[:14].isdigit():
        try:
            return datetime.strptime(s[:14], "%Y%m%d%H%M%S")
        except ValueError:
            return None
    return None


class BarPoller:
    """定时拉桥 /quote，对每根新已完成 bar 触发 on_bar 回调。

    Usage:
        poller = BarPoller(dispatcher, stock_codes=["600000.SH"], period="1m")
        poller.on_bar = lambda bar: portfolio.on_bar(bar)  # 注入回调
        # 每隔 N 秒（由上层调度）：
        poller.poll(now=datetime.now())
    """

    def __init__(
        self,
        dispatcher: HttpBridgeDispatcher,
        stock_codes: List[str],
        period: str = "1m",
        count: int = 10,
    ):
        self._dispatcher = dispatcher
        self._stock_codes = list(stock_codes)
        self._period = period
        self._count = count
        # 已触发的最新 bar 时间（防同一 bar 重复触发）
        self.last_bar_time: Optional[datetime] = None
        # 回调：每根新已完成 bar 触发一次，参数为 BarEvent
        self.on_bar: Callable[[BarEvent], None] = lambda bar: None

    def poll(self, now: Optional[datetime] = None) -> List[BarEvent]:
        """拉一次 /quote，触发所有新已完成 bar，返回触发的 BarEvent 列表。

        now: 当前时间（用于 bar 完成检测）；不传则用 datetime.now()。
        桥不可用 → 抛 BridgeUnavailableError（交上层暂停交易，不吞异常）。
        """
        if now is None:
            now = datetime.now()

        # 按 bar 时间收集 {bar_time: {stock_code: ohlcv}}，多股票同时间合并为一根 BarEvent
        by_time: Dict[datetime, Dict[str, dict]] = {}
        for code in self._stock_codes:
            bars = self._dispatcher.query_quote(code, period=self._period, count=self._count)
            for bar in bars:
                bt = parse_bar_time(bar.get("stime"))
                if bt is None:
                    continue
                # bar 完成检测：stime > now = 进行中，忽略
                if bt > now:
                    continue
                # 只触发 > last_bar_time 的新 bar
                if self.last_bar_time is not None and bt <= self.last_bar_time:
                    continue
                by_time.setdefault(bt, {})[code] = self._to_ohlcv(bar)

        # 按时间排序触发（旧→新），每根推进 last_bar_time
        triggered: List[BarEvent] = []
        for bt in sorted(by_time):
            bar_event = BarEvent(stocks=by_time[bt], bar_time=bt)
            self.last_bar_time = bt
            try:
                self.on_bar(bar_event)
            except Exception:
                # 回调异常不阻断后续 bar 处理，但向上层暴露
                raise
            triggered.append(bar_event)
        return triggered

    @staticmethod
    def _to_ohlcv(bar: dict) -> dict:
        """桥 bar dict → BarEvent.stocks 用的 OHLCV dict（Decimal 化，与回测一致）。"""
        def _d(key):
            v = bar.get(key)
            if v is None or v == "":
                return Decimal("0")
            try:
                return Decimal(str(v))
            except Exception:
                return Decimal("0")

        vol = bar.get("volume")
        try:
            vol = int(float(vol)) if vol is not None else 0
        except Exception:
            vol = 0
        return {
            "open": _d("open"),
            "high": _d("high"),
            "low": _d("low"),
            "close": _d("close"),
            "volume": vol,
        }
