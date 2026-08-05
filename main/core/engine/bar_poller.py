"""BarPoller — 实盘行情通道(0009 切片3)。

定时拉桥 GET /quote → 用**两次拉取的相对变化**判定 bar 完成 → 只触发新完成的 bar
→ 合并多股票为一根 BarEvent → 驱动 Portfolio.on_bar。

★ 不依赖任何绝对时钟(不用 iQuant 时间、不用本机时间)★
bar 完成判定:一根 bar 不再是「该股票最新一根」时,它就完成了。
  第 N 次拉取:  [..., 10:08, 10:09]           ← 10:09 最新(进行中)
  第 N+1 次:    [..., 10:08, 10:09, 10:10]   ← 10:10 出现,10:09 退居第二 → 10:09 完成,触发
判定只比较两次拉取的 bar stime 相对变化,不碰 now / 服务器时间 / 本机时间,
彻底消除部署时区与本机时钟漂移导致的「哑火 / 未来函数」风险——
Core 部署在 UTC、本机时钟跑偏、iQuant 客户端机时钟跑偏,均不影响判定。

★ 多股票按 code 独立判定完成 ★
每只股票有各自的 latest,该股票的 bar < 其 latest 才算完成。
不用全局 max stime —— 否则快股票的进度会把慢股票的「最新 bar」误判为已完成。
每 code 独立维护 last_completed,避免快股票把全局水位推高导致慢股票漏触发。
同一时间戳、各股票已完成的 bar 合并为一根 BarEvent.stocks。

桥 /quote 返回(live/bridge/iquant_bridge.py get_quote):
  {"ok": True, "data": {code: [bar, ...]}}
每 bar = DataFrame reset_index().to_dict("records"),字段含 stime(bar 结束时间,
yyyymmddHHMMSS)/time(毫秒或秒时间戳)/open/high/low/close/volume。
parse_bar_time 优先 stime(14 位串),次选 time(按 Asia/Shanghai +8 显式转换,
不依赖本机时区),兼容验证脚本观察到的字段名。

5m 是原生周期直接拉(0009 §5.1 验证定案,不做 1m→5m 聚合)。
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from .event import BarEvent
from .http_bridge_dispatcher import HttpBridgeDispatcher

# Asia/Shanghai 固定时区(时间戳路径显式转换用,不依赖本机时区)
_CST = timezone(timedelta(hours=8))


def parse_bar_time(bar: dict) -> Optional[datetime]:
    """桥 bar dict → bar 结束时间 datetime。

    优先 stime(yyyymmddHHMMSS 字符串,bar 结束时间,北京时间,无时区问题);
    次选 time / Time(13 位毫秒或 10 位秒时间戳,按 +8 显式转,不依赖本机);
    兼容 time 为 14 位串的情况。非法 / 空 → None。

    注:相对变化方案下,两次 poll 用同一规则解析,时区偏移恒定,不影响比较;
    仍按北京时间解析是为了 BarEvent.bar_time 语义与策略公式一致。
    """
    if not isinstance(bar, dict):
        return None

    def _parse_14digit(s) -> Optional[datetime]:
        s = str(s).strip()
        if len(s) >= 14 and s[:14].isdigit():
            try:
                return datetime.strptime(s[:14], "%Y%m%d%H%M%S")
            except ValueError:
                return None
        return None

    # 1. stime(bar 结束时间字符串)
    st = bar.get("stime")
    if st:
        t = _parse_14digit(st)
        if t is not None:
            return t

    # 2. time / Time(时间戳或 14 位串)
    t_raw = bar.get("time")
    if t_raw is None:
        t_raw = bar.get("Time")
    if t_raw is not None:
        t = _parse_14digit(t_raw)
        if t is not None:
            return t
        try:
            ts = float(str(t_raw).strip())
            if ts > 1e12:               # 13 位毫秒时间戳
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=_CST).replace(tzinfo=None)
        except (ValueError, OSError):
            return None

    return None


class BarPoller:
    """定时拉桥 /quote,用相对变化判定 bar 完成,对每根新完成 bar 触发 on_bar。

    无绝对时钟依赖:bar 是否完成,看它是否从「最新」退居第二(下次拉取出现更新的 bar)。
    多股票按 code 独立判定完成(各自 latest),再按时间合并为 BarEvent。
    首次拉取建立基线不触发(不回放历史 bar);之后每次触发「上次之后新完成」的 bar。

    Usage:
        poller = BarPoller(dispatcher, stock_codes=["600000.SH"], period="1m")
        poller.on_bar = lambda bar: portfolio.on_bar(bar)  # 注入回调
        # 每隔 N 秒(由上层调度):
        poller.poll()
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
        # 是否已建立基线(首次 poll 只记录,不触发)
        self._initialized: bool = False
        # 每 code 已见过的最高【完成】bar 时间(防重复触发 + 标记进度)
        self._last_completed: Dict[str, Optional[datetime]] = {}
        # 回调:每根新完成 bar 触发一次,参数为 BarEvent
        self.on_bar: Callable[[BarEvent], None] = lambda bar: None

    @property
    def last_completed_stime(self) -> Optional[datetime]:
        """所有 code 中最高的完成 bar 时间(观测值,供测试/监控;判定逻辑用 per-code)。"""
        vals = [t for t in self._last_completed.values() if t is not None]
        return max(vals) if vals else None

    def poll(self) -> List[BarEvent]:
        """拉一次 /quote,触发所有新完成的 bar,返回触发的 BarEvent 列表。

        无 now 参数(不依赖任何绝对时钟)。bar 完成 = 它不再是该股票最新 bar。
        多股票按 code 独立判定完成,同一时间戳的已完成 bar 合并为一根 BarEvent。
        桥不可用 → 抛 BridgeUnavailableError(交上层暂停交易,不吞异常)。
        """
        # 本轮新完成 bar,按时间合并:stime -> {code: ohlcv}
        new_by_time: Dict[datetime, Dict[str, dict]] = {}
        any_data = False

        for code in self._stock_codes:
            bars = self._dispatcher.query_quote(code, period=self._period, count=self._count)
            # 该 code 的 stime -> ohlcv
            stime_map: Dict[datetime, dict] = {}
            stimes: List[datetime] = []
            for bar in bars:
                bt = parse_bar_time(bar)
                if bt is None:
                    continue
                stimes.append(bt)
                stime_map[bt] = self._to_ohlcv(bar)
            if not stimes:
                continue
            any_data = True

            latest = max(stimes)
            # 该 code 已完成 = stime < latest(进行中那根不触发)
            completed = [t for t in stimes if t < latest]

            # 首次:建立该 code 基线,不触发(实盘启动不回放历史 bar)
            if not self._initialized:
                self._last_completed[code] = completed[-1] if completed else None
                continue

            last = self._last_completed.get(code)
            if last is None:
                new_times = completed
            else:
                new_times = [t for t in completed if t > last]

            # 推进该 code last_completed 到本次最高完成 bar
            if completed:
                self._last_completed[code] = completed[-1]

            # 收集新完成 bar 到合并字典
            for t in new_times:
                new_by_time.setdefault(t, {})[code] = stime_map[t]

        # 首次有数据 → 标记初始化完成,不触发
        if not self._initialized:
            if any_data:
                self._initialized = True
            return []

        # 按时间排序触发(旧→新),每根构造 BarEvent
        triggered: List[BarEvent] = []
        for t in sorted(new_by_time):
            bar_event = BarEvent(stocks=new_by_time[t], bar_time=t)
            self.on_bar(bar_event)
            triggered.append(bar_event)
        return triggered

    @staticmethod
    def _to_ohlcv(bar: dict) -> dict:
        """桥 bar dict → BarEvent.stocks 用的 OHLCV dict(Decimal 化,与回测一致)。"""
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
