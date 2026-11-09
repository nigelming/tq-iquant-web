"""交易日历与交易总开关（0010 后续：迁入 live 子包）。

权威交易日由桥 ``xtdata.get_trading_dates`` 提供（GET /calendar），Core 按自然年
缓存一次。判定原则：

- 周末（周六/周日）永远非交易日——A 股硬规则，不依赖数据，也不 fail-open。
- 工作日是否交易日由权威集合判定（覆盖节假日/调休）。
- **多源回退链**：主源（当前通道 ``dispatcher.query_calendar``）→ 备用源（掘金桥
  ``/calendar``，见 ``goldminer_calendar_provider``）→ 内置官方休市表（``BUILTIN_CLOSURES``）
  → fail-open（工作日默认交易日）。
- fail-open 语义保留（绝不因日历过期误挡真实交易日），但必须**可见**：WARNING
  日志 + 一条 risk 事件（每个年份最多一次，不刷屏），并经 ``status()`` 暴露到
  会话 API（``GET /api/live/sessions/{id}`` 的 ``trading_calendar`` 字段）。
- **空结果不永久缓存**：全部源失败时按 ``retry_after_seconds``（默认 900s）后再试，
  某源恢复即自动升级回权威集合，避免"空日历写进缓存后整年不复用"。
- provider 只返回"截至今天"的部分年份数据时，超出其覆盖范围（未来日期）不按
  "不在集合=休市"判定，退回内置休市表/工作日放行——防止日历截断把未来交易日
  误判为休市。
- ``is_trading_allowed(now)`` = 交易日 且 09:30 ≤ 时刻 < 15:00，作为下单总闸。

注意：这里只负责"是否允许下单"。引擎生命周期（start/stop）、14:30 估值、15:05 收盘
清扫各有自己的调度判定，不被本开关替代。

本模块为 live 专属（仅 LiveEngine 及其协作者使用，回测不依赖），故归入
core.engine.live 子包。原路径 core.engine.trading_calendar 保留 re-export shim，
外部 import 路径与测试直连无需改动。
"""
from __future__ import annotations

import inspect
import logging
import re
import time as _time
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

logger = logging.getLogger("core.engine.live.calendar")

# A 股连续竞价时段（下单总闸用，与 live_engine 的收盘守卫一致）
_MARKET_OPEN = time(9, 30)
_MARKET_CLOSE = time(15, 0)

_SHANGHAI_TZ = timezone(timedelta(hours=8))

# provider 契约：无参（主源）或接受自然年参数（可按年精确拉取）；元素为
# date / datetime / 'YYYYMMDD' / 'YYYY-MM-DD' / 'YYYY/MM/DD' / 毫秒时间戳。
CalendarProvider = Callable[..., Iterable]

# 内置官方休市表（仅列**工作日**休市；周末由硬规则覆盖，不重复收录）。
#
# 数据来源：上海证券交易所「休市安排（2026年）」
#   https://www.sse.com.cn/disclosure/dealinstruc/closed/
#   公告原文（2026 年）：
#     元旦 1/1(四)–1/3(六)；春节 2/15(日)–2/23(一)；清明 4/4(六)–4/6(一)；
#     劳动节 5/1(五)–5/5(二)；端午 6/19(五)–6/21(日)；中秋 9/25(五)–9/27(日)；
#     国庆 10/1(四)–10/7(三)。
#   与掘金桥 /calendar 实测返回值逐日比对一致（2026-11-05 取证）。
#
# 更新方式：每年 12 月沪深交易所发布次年全年休市安排后，在此追加对应年份；
# 未收录年份不做"凭记忆编造"，自动退化为 fail-open（工作日放行）并告警一次。
BUILTIN_CLOSURES: Dict[int, frozenset] = {
    2026: frozenset({
        date(2026, 1, 1), date(2026, 1, 2),                       # 元旦
        date(2026, 2, 16), date(2026, 2, 17), date(2026, 2, 18),
        date(2026, 2, 19), date(2026, 2, 20), date(2026, 2, 23),   # 春节
        date(2026, 4, 6),                                         # 清明
        date(2026, 5, 1), date(2026, 5, 4), date(2026, 5, 5),     # 劳动节
        date(2026, 6, 19),                                        # 端午
        date(2026, 9, 25),                                        # 中秋
        date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 5),
        date(2026, 10, 6), date(2026, 10, 7),                     # 国庆
    }),
}


def _accepts_year(fn: Callable) -> bool:
    """provider 是否接受自然年参数（避免用 TypeError 试探吞掉真实异常）。"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for p in sig.parameters.values():
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD, p.VAR_POSITIONAL):
            return True
    return False


def _from_epoch(value: float) -> Optional[date]:
    """秒/毫秒时间戳 → 上海时区自然日。"""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts > 1e11:  # 毫秒
        ts /= 1000.0
    try:
        return datetime.fromtimestamp(ts, tz=_SHANGHAI_TZ).date()
    except (OverflowError, OSError, ValueError):
        return None


def goldminer_calendar_provider(year: int) -> List[str]:
    """备用日历源：掘金桥 ``GET /calendar?start=YYYY-01-01&end=YYYY-12-31``。

    桥返回 ``["2026-11-02", ...]``（ISO 格式，``TradingCalendar._coerce`` 兼容）。
    桥离线/非 JSON/``ok=false`` 一律抛异常，由 TradingCalendar 继续向后回退。
    """
    from core.config import load_config
    import httpx

    cfg = load_config().get("goldminer") or {}
    base_url = str(cfg.get("base_url") or "http://127.0.0.1:7801").rstrip("/")
    timeout = float(cfg.get("request_timeout_seconds") or 30.0)
    with httpx.Client(timeout=timeout) as client:
        resp = client.get(
            base_url + "/calendar",
            params={"start": "%d-01-01" % year, "end": "%d-12-31" % year},
        )
        resp.raise_for_status()
        body = resp.json() or {}
    if not body.get("ok"):
        raise RuntimeError("goldminer calendar failed: %s" % body.get("error"))
    return list(body.get("data") or [])


class TradingCalendar:
    def __init__(
        self,
        provider: CalendarProvider,
        fallback_providers: Optional[List[Tuple[str, CalendarProvider]]] = None,
        retry_after_seconds: float = 900.0,
        clock: Optional[Callable[[], float]] = None,
    ):
        # (label, fn, takes_year)
        self._sources: List[Tuple[str, CalendarProvider, bool]] = [
            ("primary", provider, _accepts_year(provider))
        ]
        for label, fn in (fallback_providers or []):
            self._sources.append((label, fn, _accepts_year(fn)))
        self._retry_after = float(retry_after_seconds)
        self._clock = clock or _time.monotonic
        # year -> (交易日集合, 覆盖起, 覆盖止, 来源标签)
        self._by_year: Dict[int, Tuple[Set[date], date, date, str]] = {}
        # year -> 退化来源（'builtin' / 'fail_open'）
        self._degraded: Dict[int, str] = {}
        self._retry_at: Dict[int, float] = {}
        self._warned_year: Set[int] = set()
        # 退化告警事件回调（LiveEngine 接线后 → risk 事件；每自然年最多触发一次）
        self.on_degraded: Optional[Callable[[dict], None]] = None

    # ---------------- 加载 / 回退链 ----------------

    def _load_year(self, year: int) -> Optional[Tuple[Set[date], date, date, str]]:
        """返回某自然年权威交易日集合；None 表示已退化（见 ``_degraded[year]``）。"""
        if year in self._by_year:
            return self._by_year[year]
        now = self._clock()
        if year in self._degraded and now < self._retry_at.get(year, 0.0):
            return None  # 退避窗口内，不重复打源（避免慢桥拖主循环）

        reasons: List[str] = []
        for label, fn, takes_year in self._sources:
            try:
                raw = list((fn(year) if takes_year else fn()) or [])
            except Exception as exc:  # 桥离线/超时/HTTP 非 200/非 JSON
                reasons.append("%s: %s" % (label, exc))
                continue
            dates = {
                d for d in (self._coerce(item) for item in raw)
                if d is not None and d.year == year
            }
            if dates:
                self._by_year[year] = (dates, min(dates), max(dates), label)
                self._degraded.pop(year, None)
                self._retry_at.pop(year, None)
                if label != "primary":
                    logger.info(
                        "trading calendar for %s resolved by fallback source '%s' "
                        "(%d trading days)", year, label, len(dates))
                return self._by_year[year]
            reasons.append("%s: empty" % label)

        # 全部源失败/为空 → 退化：内置休市表（已收录年份）优先，否则 fail-open
        closed = BUILTIN_CLOSURES.get(year)
        source = "builtin" if closed is not None else "fail_open"
        self._degraded[year] = source
        self._retry_at[year] = now + self._retry_after
        self._warn_once(year, source, reasons)
        return None

    def _warn_once(self, year: int, source: str, reasons: List[str]) -> None:
        """退化告警：WARNING + risk 事件，每个自然年最多一次（不刷屏）。"""
        detail = "；".join(reasons) if reasons else "no provider configured"
        if source == "builtin":
            message = (
                "交易日历全部数据源不可用（%s）；%d 年退化为内置官方休市表（工作日"
                "非节假日照常放行，节假日休市），请检查桥/行情通道；恢复后自动重试"
                % (detail, year))
        else:
            message = (
                "交易日历全部数据源不可用（%s）；%d 年无内置休市表，fail-open："
                "工作日一律视为交易日（节假日可能误放行，下单由柜台拒单兜底）"
                % (detail, year))
        if year in self._warned_year:
            logger.debug("%s (year %s, already reported)", message, year)
            return
        self._warned_year.add(year)
        logger.warning("%s", message)
        cb = self.on_degraded
        if cb is not None:
            try:
                cb({
                    "rule": "trading_calendar_degraded",
                    "year": year,
                    "source": source,
                    "reasons": reasons,
                    "message": message,
                })
            except Exception:  # noqa: BLE001  告警回调失败不得影响日历判定
                logger.warning("calendar degraded callback failed", exc_info=True)

    # ---------------- 判定 ----------------

    @staticmethod
    def _coerce(item) -> Optional[date]:
        # datetime 是 date 的子类，必须先判 datetime，否则 datetime 会被原样返回，
        # dates 集合里存 datetime 导致 d(date) in dates 永为 False（交易日被误判非交易日）。
        if isinstance(item, datetime):
            return item.date()
        if isinstance(item, date):
            return item
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            return _from_epoch(item)
        text = str(item).strip()
        if not text:
            return None
        # 'YYYY-MM-DD' / 'YYYY/MM/DD' / 'YYYY.MM.DD'（掘金桥口径；可带时间后缀）
        m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
        digits = re.sub(r"\D", "", text)
        if len(digits) == 8:  # 通达信/QMT 桥口径 'YYYYMMDD'
            try:
                return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
            except ValueError:
                return None
        if len(digits) >= 14:  # 'YYYYMMDDHHMMSS'
            try:
                return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
            except ValueError:
                return None
        return None

    def is_trading_day(self, d: date) -> bool:
        # 周末硬规则（weekday(): Monday=0 .. Sunday=6）
        if d.weekday() >= 5:
            return False
        loaded = self._load_year(d.year)
        if loaded is not None:
            trading, cover_from, cover_to, _source = loaded
            if cover_from <= d <= cover_to:
                return d in trading
            # 超出数据源覆盖范围（多数桥只给到"今天"）：不按"不在集合=休市"判定，
            # 否则日历截断会把未来交易日误判为休市（甚至整月不下单）。
        closed = BUILTIN_CLOSURES.get(d.year)
        if closed is not None:
            return d not in closed  # 内置官方休市表：工作日 - 节假日
        return True  # fail-open：工作日无权威数据，放行

    def is_trading_allowed(self, now: datetime) -> bool:
        """是否允许在 ``now`` 时刻下单：交易日 且 09:30–15:00（左闭右开）。"""
        if not self.is_trading_day(now.date()):
            return False
        t = now.time()
        return _MARKET_OPEN <= t < _MARKET_CLOSE

    # ---------------- 可观测性 ----------------

    def status(self) -> dict:
        """日历来源/退化状态（会话 API 暴露"是否在用退化日历"）。"""
        years: Dict[str, dict] = {}
        for y in sorted(set(self._by_year) | set(self._degraded)):
            if y in self._by_year:
                trading, cover_from, cover_to, source = self._by_year[y]
                years[str(y)] = {
                    "source": source,
                    "degraded": False,
                    "trading_days": len(trading),
                    "coverage": [cover_from.isoformat(), cover_to.isoformat()],
                }
            else:
                years[str(y)] = {"source": self._degraded[y], "degraded": True}
        return {
            "degraded": bool(self._degraded),
            "years": years,
            "builtin_years": sorted(BUILTIN_CLOSURES),
            "retry_after_seconds": self._retry_after,
        }
