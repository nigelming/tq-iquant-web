"""BarPoller — 实盘行情通道(0009 切片3)。

定时拉桥 GET /quote → 用**两次拉取的相对变化**判定 bar 完成 → 只触发新完成的 bar
→ 合并多股票为一根 BarEvent → 驱动 Portfolio.on_bar。

★ 不依赖任何绝对时钟(不用 QMT 时间、不用本机时间)★
bar 完成判定:一根 bar 不再是「该股票最新一根」时,它就完成了。
  第 N 次拉取:  [..., 10:08, 10:09]           ← 10:09 最新(进行中)
  第 N+1 次:    [..., 10:08, 10:09, 10:10]   ← 10:10 出现,10:09 退居第二 → 10:09 完成,触发
判定只比较两次拉取的 bar stime 相对变化,不碰 now / 服务器时间 / 本机时间,
彻底消除部署时区与本机时钟漂移导致的「哑火 / 未来函数」风险——
Core 部署在 UTC、本机时钟跑偏、QMT 客户端机时钟跑偏,均不影响判定。

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
from datetime import date, datetime, time as _clock, timezone, timedelta
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

import logging

from .event import BarEvent
try:
    from .http_bridge_dispatcher import HttpBridgeDispatcher
except Exception:
    HttpBridgeDispatcher = Any  # type: ignore

logger = logging.getLogger(__name__)

# Asia/Shanghai 固定时区(时间戳路径显式转换用,不依赖本机时区)
_CST = timezone(timedelta(hours=8))

# bar 时间合法性窗口：超出即视为解析错误（如把 8 位日期当 epoch 秒 → 1970）
_BAR_YEAR_MIN = 2000
_BAR_YEAR_MAX = 2100
# 越界告警去重：(字段名, 原始值前缀) —— 同一错误形态只报一次，避免刷屏
_BAR_TIME_WARNED: set = set()

# 行情轮询窗口默认值(Asia/Shanghai,左闭右开):
#   09:15 —— 开盘前恢复轮询,给预热缓存留出补齐时间(09:30 首根 bar 不缺历史);
#   15:05 —— 收盘清扫时点之后暂停(保证 15:00 收盘那根 bar 已被 poll 到)。
# config.live.quote_polling 可覆盖;缺键/非法值均回落到这两个默认(安全默认=收盘后停)。
DEFAULT_QUOTE_POLL_RESUME = _clock(9, 15)
DEFAULT_QUOTE_POLL_PAUSE = _clock(15, 5)


def _as_bool(value: Any, default: bool = True) -> bool:
    """宽松布尔解析(兼容 YAML bool / "true" / 1);无法识别 → default。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _parse_clock(value: Any, default: _clock) -> _clock:
    """'09:15' / '9:15' / '0915' / time → time;无法解析 → default。"""
    if isinstance(value, _clock):
        return value
    text = str(value or "").strip()
    if not text:
        return default
    for fmt in ("%H:%M", "%H:%M:%S", "%H%M", "%H%M%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    logger.warning("非法行情轮询窗口时间 %r,回落默认 %s", value, default.strftime("%H:%M"))
    return default


class QuotePollingWindow:
    """收盘后停行情轮询的窗口判定(纯函数,不读时钟,now 由调用方传入)。

    交易日 + 窗口内才允许 BarPoller.poll();窗口外(收盘后/开盘前/非交易日)
    整个轮询停,避免对掘金桥 /quote 与通达信 TQ 的无谓持续压力。

    enabled=False → 永远开放(等价旧行为:全天轮询,连非交易日也轮询)。
    """

    def __init__(self, enabled: Any = True, resume_time: Any = None,
                 pause_time: Any = None):
        self.enabled = _as_bool(enabled, True)
        self.resume_time = _parse_clock(resume_time, DEFAULT_QUOTE_POLL_RESUME)
        self.pause_time = _parse_clock(pause_time, DEFAULT_QUOTE_POLL_PAUSE)

    @classmethod
    def from_config(cls, cfg: Any = None) -> "QuotePollingWindow":
        """config.live.quote_polling → 窗口。

        cfg 缺失/非 dict(未配置该键)→ 安全默认(交易日 09:15–15:05 之外不轮询)。
        """
        if not isinstance(cfg, dict):
            cfg = {}
        return cls(
            enabled=cfg.get("enabled", True),
            resume_time=cfg.get("resume_time"),
            pause_time=cfg.get("pause_time"),
        )

    def is_open(self, now: datetime) -> bool:
        """now 是否落在轮询窗口内(不含交易日判定,由调用方叠加)。"""
        if not self.enabled:
            return True
        return self.resume_time <= now.time() < self.pause_time

    def next_resume_at(self, now: datetime, is_trading_day: Callable[[Any], bool]
                       ) -> Optional[datetime]:
        """now 之后的下一个恢复时点(当日 resume 未过且为交易日 → 当日,否则向后找
        最近交易日),仅用于暂停日志的"下次恢复时间"提示。"""
        today = now.date()
        candidate = datetime.combine(today, self.resume_time)
        if candidate > now and is_trading_day(today):
            return candidate
        for offset in range(1, 11):
            day = today + timedelta(days=offset)
            if is_trading_day(day):
                return datetime.combine(day, self.resume_time)
        return None



def parse_bar_time(bar: dict) -> Optional[datetime]:
    """桥 bar dict → bar 结束时间 datetime。

    优先 stime(yyyymmddHHMMSS 字符串,bar 结束时间,北京时间,无时区问题);
    次选 time / Time(同上,含 8 位日期 YYYYMMDD——QMT 日线 stime 就是这个形态,
    取当日 00:00;13 位毫秒或 10 位秒时间戳按 +8 显式转,不依赖本机);
    兼容 time 为 14 位串的情况;末选 index(部分历史工具 DataFrame reset 的索引列,
    14 位时间戳或 8 位日期)。非法 / 空 → None。

    回归（2026-11-06）：日线 bar 的 stime 是 "20261106"（8 位日期），旧实现
    走到「数值时间戳」分支按 epoch 秒解析 → 1970-08-23 20:05:06，日终批次
    被误判为「收盘后」而拦掉全部信号。现在 8 位纯数字按日期解析，且任何
    解析结果年份不在 [2000, 2100] 一律 WARN（带原始值）并返回 None，绝不
    静默当作 1970 处理。

    注:相对变化方案下,两次 poll 用同一规则解析,时区偏移恒定,不影响比较;
    仍按北京时间解析是为了 BarEvent.bar_time 语义与策略公式一致。
    """
    if not isinstance(bar, dict):
        return None

    def _checked(dt, raw, field) -> Optional[datetime]:
        if dt is None:
            return None
        if not (_BAR_YEAR_MIN <= dt.year <= _BAR_YEAR_MAX):
            key = (field, str(raw)[:32])
            if key not in _BAR_TIME_WARNED:
                _BAR_TIME_WARNED.add(key)
                logger.warning(
                    "bar time out of range: %s=%r parsed as %s (year not in "
                    "[%d, %d]) — bar time ignored",
                    field, raw, dt, _BAR_YEAR_MIN, _BAR_YEAR_MAX)
            return None
        return dt

    def _parse_14digit(s) -> Optional[datetime]:
        s = str(s).strip()
        if len(s) >= 14 and s[:14].isdigit():
            try:
                return datetime.strptime(s[:14], "%Y%m%d%H%M%S")
            except ValueError:
                return None
        return None

    def _parse_8digit_date(s) -> Optional[datetime]:
        s = str(s).strip()
        if len(s) == 8 and s.isdigit():
            try:
                return datetime.strptime(s, "%Y%m%d")
            except ValueError:
                return None
        return None

    # 1. stime(bar 结束时间字符串；日线为 8 位日期 YYYYMMDD)
    st = bar.get("stime")
    if st:
        t = _checked(_parse_14digit(st), st, "stime")
        if t is not None:
            return t
        t = _checked(_parse_8digit_date(st), st, "stime")
        if t is not None:
            return t

    # 2. time / Time(8 位日期 / 14 位串 / 时间戳)
    t_raw = bar.get("time")
    if t_raw is None:
        t_raw = bar.get("Time")
    if t_raw is not None:
        t = _checked(_parse_14digit(t_raw), t_raw, "time")
        if t is not None:
            return t
        t = _checked(_parse_8digit_date(t_raw), t_raw, "time")
        if t is not None:
            return t
        try:
            ts = float(str(t_raw).strip())
            if ts > 1e12:               # 13 位毫秒时间戳
                ts = ts / 1000
            return _checked(
                datetime.fromtimestamp(ts, tz=_CST).replace(tzinfo=None),
                t_raw, "time")
        except (ValueError, OSError):
            return None

    # 3. index(部分历史工具 DataFrame reset 索引列;14 位时间戳或 8 位日期)
    idx_raw = bar.get("index")
    if idx_raw is not None:
        t = _checked(_parse_14digit(idx_raw), idx_raw, "index")
        if t is not None:
            return t
        s = str(idx_raw).strip()
        if len(s) >= 8 and s[:8].isdigit():
            try:
                return _checked(
                    datetime.strptime(s[:8], "%Y%m%d"), idx_raw, "index")
            except ValueError:
                return None

    return None


def to_ohlcv(bar: dict) -> dict:
    """桥 bar dict → BarEvent.stocks 用的 OHLCV dict(Decimal 化,与回测一致)。

    模块级函数(供 LiveEngine C6 边界/日终分发复用,不依赖 BarPoller 实例)。
    """
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


def latest_completed_bar(bars: list) -> Optional[dict]:
    """bars 中「最新已完成」bar(非 forming 最新一根),供 C6 边界分发取该周期 bar。

    完成规则同 BarPoller.poll:stime < 本批 latest 才算完成;取完成里时间最大的那根。
    无完成 bar → None(如开盘首根边界,该周期 bar 尚未完成,跳过等下一根)。
    """
    stime_map: Dict[datetime, dict] = {}
    for b in bars:
        t = parse_bar_time(b)
        if t is not None:
            stime_map[t] = b
    if not stime_map:
        return None
    latest = max(stime_map)
    completed = {t: b for t, b in stime_map.items() if t < latest}
    if not completed:
        return None
    target = max(completed)
    return stime_map[target]


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
        dispatcher: Any,
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
        # TQ 1.3.1:批量行情回退 warning 只记一次(防刷屏)
        self._batch_fallback_warned: bool = False
        # 暂停→恢复边界标记:下一次 poll 对「尚无水位」的代码只建基线不触发
        # (防收盘后暂停期间扩池的新代码把窗口里的隔夜 bar 当盘中触发)
        self._resume_guard: bool = False
        # 恢复当日（只允许该日及以后 bar 触发；None=不限制，见 mark_resume_boundary）
        self._resume_day: Optional[date] = None
        # 回调:每根新完成 bar 触发一次,参数为 BarEvent
        self.on_bar: Callable[[BarEvent], None] = lambda bar: None

    @property
    def stock_codes(self) -> List[str]:
        """订阅股票代码列表（只读视图，供 LiveEngine 行情订阅范围查询，审计 #30）。"""
        return self._stock_codes

    def add_codes(self, codes: List[str]) -> None:
        """动态扩池（账户对齐：外部持仓导入后纳入行情轮询/风控驱动）。

        新代码初始化独立水位（_last_completed=None，下次 poll 有数据时按
        「基线」处理——TQ 合成 bar 源无历史回放，天然不触发存量 bar；
        K 线源（iquant）首个窗口的已完成 bar 会作为新触发推入，量级
        ≤ count 窗口，可接受）。已存在的代码忽略。"""
        for code in codes:
            c = str(code or "").strip()
            if c and c not in self._stock_codes:
                self._stock_codes.append(c)
                self._last_completed.setdefault(c, None)

    @property
    def period(self) -> str:
        """轮询周期（透传 bars 的周期）。LiveEngine 用它区分 bars_by_code 来源：
        本周期 = BarPoller 透传（count 窗口判完成用）；其他周期 = 边界分发预拉。"""
        return self._period

    def mark_resume_boundary(self, day: Optional[date] = None) -> None:
        """暂停→恢复边界（收盘后停轮询 → 次日恢复时由 LiveEngine 调用）。

        恢复首轮：
        1. 只接受 ``day``（恢复当日）及之后的 bar 参与触发——窗口里的隔夜/历史
           bar（如暂停前最后几次 poll 未覆盖到的昨日尾盘 bar）一律丢弃，不会被
           当成盘中信号驱动风控下单；
        2. 对「尚无完成水位」的代码只建基线、不触发：这些代码通常是暂停期间由
           对账导入扩池（add_codes）进来的，普通规则（last is None → 全部触发）
           会把旧价格当盘中。
        已有水位的代码不受影响：`t > last` 挡掉隔夜 bar，且不会丢当日首根。
        水位推进仍取「过滤后的完成集合」，不会把未触发的隔夜 bar 推成水位。
        """
        self._resume_guard = True
        self._resume_day = day

    @property
    def last_completed_stime(self) -> Optional[datetime]:
        """所有 code 中最高的完成 bar 时间(观测值,供测试/监控;判定逻辑用 per-code)。"""
        vals = [t for t in self._last_completed.values() if t is not None]
        return max(vals) if vals else None

    def _fetch_bars_by_code(self) -> Dict[str, list]:
        """一轮行情拉取：dispatcher 支持批量（supports_batch_quote，仅 TdxTqDispatcher）
        时一次 query_quotes_batch 取全部股票；否则逐只 query_quote。

        返回 {code: [bar, ...]}。批量调用异常/返回空 → 回退逐只路径（与逐只
        100% 等价，QMT/ths 行为不变），warning 只记一次防刷屏。
        """
        if getattr(self._dispatcher, "supports_batch_quote", False):
            try:
                batch = self._dispatcher.query_quotes_batch(
                    self._stock_codes, self._period, self._count)
                if batch:
                    return batch
                reason = "批量返回空"
            except Exception as e:  # noqa: BLE001
                reason = "批量拉取异常：%s" % e
            if not self._batch_fallback_warned:
                self._batch_fallback_warned = True
                logger.warning("批量行情不可用（%s），回退逐只拉取（仅提示一次）", reason)
        bars_by_code: Dict[str, list] = {}
        for code in self._stock_codes:
            bars_by_code[code] = self._dispatcher.query_quote(
                code, period=self._period, count=self._count)
        return bars_by_code

    def poll(self) -> List[BarEvent]:
        """拉一次行情,触发所有新完成的 bar,返回触发的 BarEvent 列表。

        无 now 参数(不依赖任何绝对时钟)。bar 完成 = 它不再是该股票最新 bar。
        多股票按 code 独立判定完成,同一时间戳的已完成 bar 合并为一根 BarEvent。
        桥不可用 → 抛 BridgeUnavailableError(交上层暂停交易,不吞异常)。
        dispatcher 支持批量时一轮一次批量拉取(TQ 1.3.1),后续处理与逐只一致。
        """
        # 本轮新完成 bar,按时间合并:stime -> {code: ohlcv}
        new_by_time: Dict[datetime, Dict[str, dict]] = {}
        # 本轮各 code 拉到的原始 bar 列表(count 窗口,含 forming 最新一根)——挂到 BarEvent.
        # bars_by_code 透传,注入并入预热缓存复用,消除 1m 双拉(判完成与算公式共用一次拉取)。
        raw_by_code: Dict[str, list] = {}
        any_data = False

        # 一轮行情拉取:批量或逐只(见 _fetch_bars_by_code),后续处理两条路径完全一致
        fetched = self._fetch_bars_by_code()

        for code in self._stock_codes:
            bars = fetched.get(code, [])
            raw_by_code[code] = bars
            # 该 code 的 stime -> ohlcv
            stime_map: Dict[datetime, dict] = {}
            stimes: List[datetime] = []
            for bar in bars:
                bt = parse_bar_time(bar)
                if bt is None:
                    continue
                stimes.append(bt)
                stime_map[bt] = to_ohlcv(bar)
            if not stimes:
                continue
            any_data = True

            latest = max(stimes)
            if getattr(self._dispatcher, "bars_explicitly_completed", False):
                # 数据源明确声明「返回的每一根都已是闭合 bar」（快照合成器只在
                # 采样时刻 >= 该分钟终点后才把 forming 移入 completed，forming
                # 永不出现在返回窗口）→ 最新一根也已闭合，直接纳入本轮完成集合。
                # 幂等仍由下方 per-code _last_completed + `t > last` 保证。
                completed = list(stime_map)
            else:
                # 相对变化规则：最新一根视为进行中（可能仍在合成/未收盘）
                completed = [t for t in stimes if t < latest]

            # 暂停→恢复首轮：隔夜/历史 bar 不参与触发（只认恢复当日及以后），
            # 防止窗口里残留的昨日尾盘 bar 被当成盘中信号（见 mark_resume_boundary）
            baseline_from = completed
            if self._resume_day is not None:
                completed = [t for t in completed if t.date() >= self._resume_day]

            # 首次:建立该 code 基线,不触发(实盘启动不回放历史 bar)
            if not self._initialized:
                self._last_completed[code] = max(baseline_from) if baseline_from else None
                continue

            last = self._last_completed.get(code)
            if last is None and self._resume_guard:
                # 暂停→恢复首轮：无水位代码（暂停期扩池）以整个窗口最高完成 bar
                # 建基线（含隔夜），本轮不触发
                if baseline_from:
                    self._last_completed[code] = max(baseline_from)
                continue
            if last is None:
                new_times = completed
            else:
                new_times = [t for t in completed if t > last]

            # 推进该 code last_completed 到本次最高完成 bar（max 而非 [-1]：
            # 水位不能依赖 provider 返回顺序，降序返回时 [-1] 会把水位拉到最早一根，
            # 导致旧 bar 被重复触发）
            if completed:
                self._last_completed[code] = max(completed)

            # 收集新完成 bar 到合并字典
            for t in new_times:
                new_by_time.setdefault(t, {})[code] = stime_map[t]

        # 恢复首轮守护一次性：本轮**有数据**才清除（无数据/异常保留到下一轮，仍安全）。
        # 09:15 恢复时 1m 源常返回空窗口，若此时清掉守护，09:31 首批真实 bar 到来时
        # 暂停期扩池的无水位代码会把整个 count 窗口（含隔夜）当盘中触发。
        # _resume_day（最早可触发日）保留到下一次恢复重设：恢复首轮之后窗口里若
        # 还残留隔夜 bar，也不会被当成盘中触发。
        if any_data:
            self._resume_guard = False

        # 首次有数据 → 标记初始化完成,不触发
        if not self._initialized:
            if any_data:
                self._initialized = True
            return []

        # 按时间排序触发(旧→新),每根构造 BarEvent(带周期,C6 节拍过滤用)。
        # bars_by_code 只含该 bar.stocks 对应 code 的本轮原始 bars(透传给注入复用)。
        triggered: List[BarEvent] = []
        for t in sorted(new_by_time):
            provided = {c: raw_by_code.get(c, []) for c in new_by_time[t]}
            bar_event = BarEvent(
                stocks=new_by_time[t], bar_time=t, period=self._period,
                bars_by_code=provided,
            )
            self.on_bar(bar_event)
            triggered.append(bar_event)
        return triggered

    def reset_baseline(self) -> None:
        """E8:离线恢复后重建基线——下次 poll 走首次基线分支,只记录水位不触发。

        清空每 code 已见过的完成水位,恢复后拉到的最新完成 bar 直接建基线,
        离线期间错过的 bar 自然丢弃(不补触发:补触发=过时信号+现价成交,价格错位)。
        """
        self._initialized = False
        self._last_completed = {}
