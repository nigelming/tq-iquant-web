"""LiveEngine — 实盘端到端引擎（0009 切片4）。

定时拉 Bar（BarPoller）→ Portfolio.on_bar 取信号/风控 → HttpBridgeDispatcher 真实下单
→ 落 live_orders/live_trades。引擎核心（Portfolio/ExecutionEngine）复用回测逻辑，
仅注入 HttpBridgeDispatcher + LiveT1Checker 替换 Simulated*。

成交时机：当根 bar 立即成交（实盘无「下一 bar」可等），桥受理即视为成交，
成交价首期用 bar.close（OrderEvent.price），真实价切片5 /deals 回填。

持仓恢复（recover）：Core 重启后从 live_trades 重放，重建 StrategyContext.positions
与 Account.cash，保证恢复后的持仓结构与在线时一致。虚拟现金以成本计（§93）。
"""
import asyncio
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from typing import AsyncIterator, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from .portfolio import Portfolio
from .position import Position
from .execution_engine import ExecutionEngine, LiveT1Checker
from .event import BarEvent, OrderEvent, TradeEvent
from .http_bridge_dispatcher import (
    HttpBridgeDispatcher,
    BridgeUnavailableError,
    BridgeOrderRejected,
)
from .bar_poller import BarPoller, parse_bar_time, to_ohlcv, latest_completed_bar
from core.models import LiveOrder, LiveTrade, LiveSessionPortfolio
from core.tq.formula import TQFormula
from tq_iquant_shared.constants import SignalType, TradeType

logger = logging.getLogger(__name__)

# Asia/Shanghai 固定时区——实盘日终 (14:30) 判定/时间记录按上海时间，
# 不依赖本机时区（Core 部署 UTC 服务器时本机 14:30 ≠ 上海 14:30，日终会哑火）。
_CST = timezone(timedelta(hours=8))

# 实盘日终判定时点（E5/E6 update_daily + C6(B/C) 1d 快照/1w/1mon 注入共用）。
# 上海 14:30 收盘后驱动。提为常量消除魔法数字（审计 #33）。
_DAILY_CLOSE_TIME = (14, 30)


def now_shanghai() -> datetime:
    """当前上海时间（naive，已剥时区）。实盘固有时点判定专用。

    naive 与引擎内其余 datetime（均 naive）比较一致；aware 与 naive 比较会抛 TypeError。
    """
    return datetime.now(tz=_CST).replace(tzinfo=None)


# TQ 公式输出中需跳过的非变量键（同 backtest._FORMULA_META_KEYS）
_FORMULA_META_KEYS = ("Date", "ErrorId", "Error", "Time")

# C6(C)：1w/1mon 走通达信启动/日终注入（桥端 xtdata 拉不到），_fill_signal_cache 跳过不拉桥
_STARTUP_ONLY_PERIODS = ("1w", "1mon")


def periods_on_boundary(bar_time: Optional[datetime]) -> List[str]:
    """1m bar 结束时刻 → 命中的边界周期列表（可累积，只读 bar stime，不引入本机时钟）。

    minute%5==0→5m、%15→15m、%30→30m、minute==0→1h。可累积：10:30 → [5m,15m,30m]，
    11:00 → [5m,15m,30m,1h]。非边界时刻（如 10:03）→ []。
    """
    if bar_time is None:
        return []
    result: List[str] = []
    minute = bar_time.minute
    if minute % 5 == 0:
        result.append("5m")
    if minute % 15 == 0:
        result.append("15m")
    if minute % 30 == 0:
        result.append("30m")
    if minute == 0:
        result.append("1h")
    return result


def _to_int(val) -> int:
    """数值转 int（公式 trigger_value）；NaN/None/无法解析 → 0。同 backtest._to_int。"""
    if val is None:
        return 0
    if isinstance(val, bool):
        return int(val)
    if isinstance(val, (int, float)):
        try:
            if math.isnan(val):
                return 0
        except (TypeError, ValueError):
            pass
        return int(val)
    try:
        return int(Decimal(str(val)))
    except (ValueError, ArithmeticError):
        return 0


class LiveEngine:
    def __init__(
        self,
        session_id: int,
        portfolios: List[Portfolio],
        dispatcher: HttpBridgeDispatcher,
        bar_poller: BarPoller,
        db_session_factory: Callable[[], Session],
        poll_interval: float = 60.0,
        deals_poll_interval: float = 5.0,
        stream_ping_interval: float = 30.0,
        tq_formula: Optional[TQFormula] = None,
        formula_by_strategy: Optional[Dict[int, str]] = None,
        formula_count: int = 200,
        formula_count_by_name: Optional[Dict[str, int]] = None,
        code_period_count: Optional[Dict[tuple, int]] = None,
    ):
        self.session_id = session_id
        self.portfolios = portfolios
        self._dispatcher = dispatcher
        self._bar_poller = bar_poller
        self._db_session_factory = db_session_factory
        self._poll_interval = poll_interval
        # G5：/deals 成交回报轮询独立节拍（默认 5s，比主循环 60s 更短）——成交秒级回报，
        # 持仓/资金反馈要近实时，不跟 bar 拉取同频。
        self._deals_poll_interval = deals_poll_interval
        # 实盘执行引擎：复用回测 ExecutionEngine，注入桥 dispatcher + 实盘 T+1 检查
        # F5：t1_checker 持每 bar 刷新的桥可用表（SELL 减仓上限用 m_nCanUseVolume）
        self._t1_checker = LiveT1Checker()
        self._engine = ExecutionEngine(dispatcher, self._t1_checker)

        # 公式注入（0010）：tq_formula 封装内存注入链路；formula_by_strategy 预加载
        # {strategy_id: formula_name}，避免每 bar 查库；formula_count 为注入历史根数
        # （1m/5m 默认 200，够均线预热；不足时调大）。
        self._tq_formula = tq_formula
        self._formula_by_strategy: Dict[int, str] = formula_by_strategy or {}
        self._formula_count = formula_count
        # #27→#28：count 按公式配（Formula.formula_count），同公式恒定 → C4 去重 key
        # (code, period, formula) 无需 count 进 key。_formula_count 作全局兜底（老调用不破）。
        self._formula_count_by_name: Dict[str, int] = formula_count_by_name or {}
        # 每周期预拉最大 count：该周期策略所用公式的最大 formula_count——边界/日终分发
        # 预拉的 bars 够该周期最长公式（count 不够时注入会缺历史，信号 NaN 静默失效）。
        self._period_count: Dict[str, int] = {}
        # 实例所有策略的周期集合（含无公式策略，含 1d/1w/1mon）——_dispatch_period_bar
        # 边界分发 guard 用：只拉实例确有策略的周期，挡掉 periods_on_boundary 纯算术
        # 带出但实例无人用的周期（如 14:30 的 15m——minute%15==0 触发，却无 15m 策略，
        # 白拉 17 只 count=200 后 period 过滤全跳过）。比 _period_count 更宽：后者只收
        # 有公式映射的策略周期，无公式的 30m 策略靠此集合保住风控单的边界驱动。
        self._strategy_periods: set = set()
        for _p in portfolios:
            for _ctx in _p.strategies:
                self._strategy_periods.add(_ctx.period)
                _name = self._formula_by_strategy.get(_ctx.strategy_id)
                if not _name:
                    continue
                _cnt = self._formula_count_by_name.get(_name, self._formula_count)
                if _cnt > self._period_count.get(_ctx.period, 0):
                    self._period_count[_ctx.period] = _cnt

        # 已分发过周期边界的 1m stime 集合——同根 bar 二次触发时挡掉重复周期分发。
        # BarPoller 按 code 独立判定完成：慢股票在下一轮 poll 才完成同一 stime
        # （真机 14:30 被二次驱动，15m 二次白拉）。周期分发全局重拉全 stock_codes +
        # 周期策略二次求值 = 白拉；1m 策略不受影响（每轮 bar.stocks 只含当轮新完成
        # 股票，慢股票仍在其完成那轮被驱动求值）。stime 含日期，跨日无碰撞。
        self._dispatched_boundaries: set = set()

        # 按 (code, period) 的预热/分发最大 count：该股票该周期所有公式 formula_count 最大值
        # （跨组合跨策略，由 _build_engine 算好传入）。比 _period_count（全局按周期）更细：
        # 不同股票该周期所需根数可能不同（如 C 只需 100、A 需 200），按需拉不浪费。
        # _period_count 保留作"只知 period 不知 code"的兜底。无公式兜底 formula_count(200)。
        self._code_period_count: Dict[tuple, int] = code_period_count or {}

        # 预热缓存：(code, period) -> {"bars": [...], "last_stime": datetime, "count": int}
        # 启动 _preheat() 填充（拉 code_period_count[(code,period)] 根历史）；
        # 运行期 _get_bars_with_increment 读它 + 增量拉新 bar 拼接（省去每 bar 全量重拉）；
        # 离线恢复 _tick_main 清空 → 下次走全量重建。跨 bar 生命周期（不像 df_cache 每 bar 重建）。
        self._preheat_cache: Dict[tuple, dict] = {}

        # 信号缓存：(strategy_id, stock_code, bar_time) -> [{"name": str, "value": int}]
        # 风控信号（止损/止盈/移动止损）由 Portfolio._check_risks 直接生成，无需缓存；
        # 公式信号（OPEN/ADD/REDUCE/CLOSE）需缓存命中才触发——_fill_signal_cache 在
        # 每根 bar 前拉历史 → 内存注入算公式 → 填此 dict。测试可直接预置以验证下单链路。
        self.signal_cache: Dict = {}

        self._running = False
        self._task: Optional[asyncio.Task] = None
        # G5：/deals 回填轮询的独立任务（与主循环并行，见 _deals_loop）
        self._deals_task: Optional[asyncio.Task] = None
        # 审计 #3：_loop/_deals_loop 的同步阻塞 I/O（heartbeat/poll/_poll_deals，全用同步
        # httpx.Client）整轮丢到此单 worker 线程池执行——事件循环不再被阻塞 I/O 冻结。
        # max_workers=1 让两循环的 tick 严格串行：共享 _pending_orders / positions /
        # signal_cache 无并发竞争（沿用既有「同事件循环协作式」的串行语义，只是换到线程）。
        # 单 worker 还保证 cancel 后已排队的 tick 不与下一轮重叠（asyncio.Lock 在 cancel
        # 时会释放，无法保证；单 worker 天然排队）。
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1)
        # _emit 跨线程投递用：start() 时捕获运行中的事件循环；worker 线程内 _emit 经
        # call_soon_threadsafe 回到事件线程 put_nowait（asyncio.Queue 非线程安全）。
        # None = 尚未 start（直接同步调 _emit 的旧测试路径 → 直接 put_nowait）。
        self._loop_ref: Optional[asyncio.AbstractEventLoop] = None
        self._bridge_online = True
        # E5/E6：14:30 日终已算过的日期标记（每日一次，避免每轮重复调 update_daily）
        self._last_daily_date: Optional[date] = None
        # C6(B/C)：14:30 日终 1d 快照 bar + 1w/1mon 注入已驱动的日期标记（每日一次）
        self._last_daily_bar_date: Optional[date] = None
        # 切片5（I4）：Core 重启后从 DB 挂回的未完结 LiveOrder（submitted/partial），
        # 主循环 _poll_deals 据此轮询 /deals 回填。key=LiveOrder.id。
        # 运行中 _handle_bar 发单也计入、拒单弹出；_poll_deals 每轮回合重查 DB 同步（G7）。
        self._pending_orders: Dict[int, "LiveOrder"] = {}
        # G7（0011 §5.11）：最近一次 /deals 成交回报回填时点（None=尚无回填），
        # 供 session API 桥状态并入。
        self._last_backfill_time: Optional[datetime] = None
        # F5：最近处理 bar 的强引用（多组合共享同一 bar 对象 → 每 bar 只刷一次桥可用
        # 持仓；强引用防对象 id 复用导致的漏刷）。None=尚未处理任何 bar。
        self._available_bar: Optional["BarEvent"] = None
        # D4/H4：每组合已持久化的熔断计数（LiveSessionPortfolio.circuit_breaker_count）。
        # recover 预置当前值；_persist_breaker_count 只在计数变化时落库（避免每 bar 写）。
        # key=portfolio.portfolio_id。
        self._breaker_count_written: Dict[int, int] = {}
        # D3：recover 对账结果——虚拟持仓 vs 桥 /positions 按 code 比对的差异列表
        # （{code, virtual, real, diff}）。只记录/告警，不自动修正账面（首期安全）。
        self._reconcile_mismatches: List[dict] = []
        # B5：SSE 事件流——_stream_subscribers 为当前已连接客户端的订阅队列，
        # _emit 向各队列 put_nowait 广播；stream_events 注册/退订队列。
        # stream_ping_interval：流空闲无事件时的 ping 心跳间隔（design §5.6.10，30s）。
        self._stream_ping_interval = stream_ping_interval
        self._stream_subscribers: List["asyncio.Queue"] = []

    # ---------------- B5 SSE 事件流 ----------------
    def _emit(self, event_type: str, payload: dict) -> None:
        """向所有 SSE 订阅队列广播 {type, **payload}（signal/order/trade/position/risk）。

        线程安全（审计 #3）：_emit 可能从事件线程（stream_events 消费侧、同步测试直调）
        或 worker 线程（_loop/_deals_loop tick 内 _handle_bar/_backfill_order）被调。
        asyncio.Queue.put_nowait 非线程安全 → worker 线程内经 call_soon_threadsafe
        回到事件线程投递；事件线程内（或未 start、_loop_ref=None 的同步路径）直接
        put_nowait（保留既有同步测试 get_nowait 立即可见的语义）。
        队列积压（订阅端消费慢）→ 丢弃该事件（EventSource 断线重连后见最新状态，
        design §5.6.10：浏览器自动重连，无需重放）。
        """
        ev = {"type": event_type, **payload}
        loop = self._loop_ref
        if loop is None:
            # 未 start（同步测试 / start 前）：直接投递
            self._emit_to_subscribers(ev)
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            # 事件线程内：直接投递
            self._emit_to_subscribers(ev)
        else:
            # worker 线程内：回到事件线程投递（asyncio.Queue 非线程安全）
            loop.call_soon_threadsafe(self._emit_to_subscribers, ev)

    def _emit_to_subscribers(self, ev: dict) -> None:
        """实际向各订阅队列 put_nowait（仅在事件线程执行）。积压 → 丢弃不阻塞。"""
        for q in list(self._stream_subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # 积压丢弃，不阻塞引擎

    async def stream_events(self) -> AsyncIterator[dict]:
        """SSE 事件流：订阅引擎事件队列，逐条 yield；空闲超 ping 间隔 yield ping 心跳。

        每个 /stream 连接调用一次（/stream 端点 async for 消费）。连接结束（aclose）
        → finally 退订。引擎停止（_running=False）→ 队列空则流结束（端点侧转 ping-only）。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._stream_subscribers.append(q)
        try:
            while self._running or not q.empty():
                try:
                    ev = await asyncio.wait_for(
                        q.get(), timeout=self._stream_ping_interval
                    )
                except asyncio.TimeoutError:
                    ev = {"type": "ping", "time": now_shanghai().isoformat()}
                yield ev
        finally:
            if q in self._stream_subscribers:
                self._stream_subscribers.remove(q)

    # ---------------- 预热 + 增量拼接（拉取优化）----------------
    def _preheat(self) -> None:
        """启动预热：对每个 (code, period) 拉 code_period_count 根历史存 _preheat_cache。

        只预热 _code_period_count 里的 (code,period)（实例真实有策略的，按需不浪费），
        跳过 1d/1w/1mon（1d 不预热走日终 _maybe_daily_bars；1w/1mon 走通达信 _inject_startup_periods）。
        单 (code,period) 拉取失败不阻断启动（log warn，运行期该 key 走 _get_bars_with_increment
        的"缓存未命中全量补"自愈）。启动一次性同步调用，在 start() 里 _inject_startup_periods 之后。
        """
        for (code, period), count in self._code_period_count.items():
            if period in ("1d", "1w", "1mon"):
                continue
            try:
                bars = self._dispatcher.query_quote(code, period=period, count=count)
            except BridgeUnavailableError:
                logger.warning("preheat failed (bridge unavailable) %s %s", code, period)
                continue
            except Exception:  # noqa: BLE001
                logger.exception("preheat failed %s %s", code, period)
                continue
            if not bars:
                continue
            self._preheat_cache[(code, period)] = self._make_cache_entry(bars, count)

    def _make_cache_entry(self, bars: list, count: int) -> dict:
        """bars → 排序截断到 count 根 + 算 last_stime，构造 _preheat_cache 条目。"""
        bars = self._sort_and_cap(bars, count)
        return {"bars": bars, "last_stime": self._max_stime(bars), "count": count}

    def _get_bars_with_increment(self, code: str, period: str, count: int) -> list:
        """读预热缓存历史 + 增量拉新 bar 拼接，返回 count 根窗口（拉取优化核心）。

        1) 缓存命中：增量拉 query_quote(count=INCREMENT_COUNT) 筛 stime > cache.last_stime
           的新 bar，拼到 cache.bars 末尾，排序截断保持 count 长；无新 bar 直接返缓存（最省）。
        2) 缓存未命中（预热失败/离线清缓存后）：全量拉 count 根回填缓存（异常/首次路径，
           不背正常增量的负担）。
        桥拉取抛 BridgeUnavailableError 向上传播（交 _on_bar/_dispatch_period_bar 置离线）。
        """
        INCREMENT_COUNT = 10  # 增量拉取根数，够覆盖正常单边界增量（1-2 根）；离线恢复走清缓存全量重建
        cache = self._preheat_cache.get((code, period))
        if cache is None:
            bars = self._dispatcher.query_quote(code, period=period, count=count)
            if bars:
                self._preheat_cache[(code, period)] = self._make_cache_entry(bars, count)
            return bars
        # 缓存存在但请求 count > 缓存 count：缓存历史不够长公式的窗口 → 升级全量拉 count 根
        # （同 _fetch_cached_bars 升级语义：count 不够会缺历史，长均线 NaN 静默失效）。
        if count > cache["count"]:
            bars = self._dispatcher.query_quote(code, period=period, count=count)
            if bars:
                self._preheat_cache[(code, period)] = self._make_cache_entry(bars, count)
            return bars
        new_bars = self._dispatcher.query_quote(code, period=period, count=INCREMENT_COUNT)
        last = cache["last_stime"]
        fresh = [b for b in new_bars if self._bar_stime(b) is not None
                 and (last is None or self._bar_stime(b) > last)]
        if not fresh:
            return cache["bars"]
        merged = self._sort_and_cap(cache["bars"] + fresh, count)
        cache["bars"] = merged
        cache["last_stime"] = self._max_stime(merged)
        return merged

    @staticmethod
    def _bar_stime(bar: dict) -> Optional[datetime]:
        """bar → 结束时间 datetime（复用 parse_bar_time，兼容 stime/time/index）。"""
        return parse_bar_time(bar)

    @staticmethod
    def _sort_and_cap(bars: list, count: int) -> list:
        """按 bar stime 升序排序，截断保留最新 count 根（去重同 stime）。"""
        timed = [(parse_bar_time(b), b) for b in bars]
        seen: Dict[datetime, dict] = {}
        for bt, b in timed:
            if bt is not None and bt not in seen:
                seen[bt] = b
        ordered = [b for _, b in sorted(seen.items())]
        return ordered[-count:] if count > 0 else ordered

    @staticmethod
    def _max_stime(bars: list) -> Optional[datetime]:
        """bars 中最新 bar 的 stime（空 → None）。"""
        stimes = [parse_bar_time(b) for b in bars]
        stimes = [t for t in stimes if t is not None]
        return max(stimes) if stimes else None

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        """起 asyncio 循环任务，绑定 BarPoller.on_bar 回调。"""
        if self._running:
            return
        self._running = True
        # 审计 #3：捕获运行中的事件循环，供 _emit 跨线程 call_soon_threadsafe 回投。
        self._loop_ref = asyncio.get_running_loop()
        # 审计 #3：stop() 会 shutdown executor（释放非 daemon worker 线程，防进程退出
        # 挂起）；若引擎被重启（同实例二次 start），旧 executor 已 shutdown → 重建一个。
        # 每次 start 重建开销可忽略（1 线程，session 生命周期内仅一次）。
        if self._executor is not None:
            try:
                self._executor.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
        self._executor = ThreadPoolExecutor(max_workers=1)
        # C6(C)：启动时通达信注入 1w/1mon 策略信号（桥端 xtdata 拉不到，仅此通路）。
        # 一次同步 TDX 计算（get_tdx_lock 串行），阻塞事件循环可接受（启动一次性）。
        self._inject_startup_periods(
            datetime.combine(now_shanghai().date(), datetime.min.time())
        )
        # 预热 1m/5m/15m/30m/1h 历史 bar 到 _preheat_cache（启动一次性同步拉取）。
        # 运行期 _get_bars_with_increment 读缓存 + 增量拼接，省去每 bar/每边界全量重拉。
        # 必须在 _loop 起来前完成（否则首个 bar 触发时缓存未就绪）；与 _inject_startup_periods
        # 同为启动一次性，并列。单 (code,period) 失败不阻断，运行期自愈。
        self._preheat()
        self._bar_poller.on_bar = self._on_bar
        self._task = asyncio.create_task(self._loop())
        # G5：独立 /deals 回填轮询（5s 节拍，不随主循环 60s 拉 bar 一起）
        self._deals_task = asyncio.create_task(self._deals_loop())

    async def stop(self) -> None:
        """停循环任务。"""
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("live loop task ended with error on stop")
            self._task = None
        if self._deals_task is not None:
            self._deals_task.cancel()
            try:
                await self._deals_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                logger.exception("deals loop task ended with error on stop")
            self._deals_task = None
        # 审计 #3：停止后清空 loop 引用——worker 线程已不再产生 _emit；
        # 之后若再有同步直调 _emit（测试 / 外部），走「无 loop」直接 put_nowait 路径。
        self._loop_ref = None
        # 审计 #3：释放单 worker 线程（ThreadPoolExecutor 默认非 daemon，不 shutdown 会
        # 在进程退出时挂起 join）。wait=False：正在跑的 tick 是 loopback HTTP，瞬间完成，
        # 不阻塞 stop；任务已 cancel，不会再提交新 tick。
        if self._executor is not None:
            self._executor.shutdown(wait=False)

    async def _loop(self) -> None:
        """主循环：心跳 → 拉 bar → 日终 → sleep。桥离线则暂停下单、标状态，不抛异常。

        /deals 成交回报回填不在本循环（G5）：独立 _deals_loop 5s 节拍处理，
        免得成交秒级回报被 60s 拉 bar 节拍拖慢。

        审计 #3：同步阻塞 I/O（heartbeat/poll/_maybe_daily_*，全用同步 httpx.Client）
        整轮丢到单 worker 线程池执行（_tick_main），事件循环只负责调度 + sleep——
        不再被 60s 拉 bar / 桥超时阻塞，HTTP 请求与 SSE 流不再冻结。
        """
        while self._running:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    self._executor, self._tick_main
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                # _tick_main 内部已兜底各类异常并记日志；此处的 executor 层异常兜底防漏
                logger.exception("live loop executor error")
            await asyncio.sleep(self._poll_interval)

    def _tick_main(self) -> None:
        """_loop 单轮同步体（在 worker 线程执行）：心跳 → poll → 日终。

        异常在此捕获（非 CancelledError）——与原 _loop 行为一致：记日志不外抛，
        下一轮继续。BridgeUnavailableError → 置离线，本轮跳过。
        """
        try:
            # E8：心跳前的在线状态，用于离线→在线转场时重建基线
            was_online = self._bridge_online
            if not self._dispatcher.heartbeat():
                self._bridge_online = False
                logger.warning("bridge offline, pause trading (session %s)", self.session_id)
                return
            self._bridge_online = True
            if not was_online:
                # E8：离线恢复 → 重建基线，跳过离线期间错过的 bar（不补触发）
                self._bar_poller.reset_baseline()
                # 清预热缓存：离线期间缓存 last_stime 已过期，增量拉 10 根可能接不上
                # （断档超 10 根）。清空后下次 _get_bars_with_increment 走"缓存未命中"
                # 全量拉 code_period_count 根重建——异常路径自愈，不污染正常增量路径。
                self._preheat_cache.clear()
                logger.info(
                    "bridge back online, reset poller baseline + preheat cache (session %s)",
                    self.session_id,
                )
            # poll() 内部对每根完成的 bar 触发 self._on_bar 回调
            self._bar_poller.poll()
            # E5/E6：14:30 日终一次 update_daily（日内亏损/熔断次日恢复推进）
            self._maybe_daily_close()
            # C6(B/C)：14:30 日终一次 1d 快照 bar + 1w/1mon 通达信注入驱动
            self._maybe_daily_bars()
        except BridgeUnavailableError as e:
            self._bridge_online = False
            logger.warning("bridge unavailable: %s, skip this round", e)
        except Exception:  # noqa: BLE001
            logger.exception("live loop unexpected error")

    async def _deals_loop(self) -> None:
        """G5：/deals 成交回报回填的独立轮询（deals_poll_interval，默认 5s）。

        成交秒级回报，独立短节拍让 LiveTrade/持仓/资金反馈近实时；主循环保持 60s 拉 bar。
        与主循环共用单 worker 线程池（审计 #3）：tick 串行，共享 _pending_orders /
        positions 无并发竞争（沿用原「同事件循环协作式」串行语义）。_poll_deals 内部
        已兜底桥离线/查失败。
        """
        while self._running:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    self._executor, self._tick_deals
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("deals loop executor error")
            await asyncio.sleep(self._deals_poll_interval)

    def _tick_deals(self) -> None:
        """_deals_loop 单轮同步体（在 worker 线程执行）：查未完结单 → 回填 /deals。

        异常在此捕获（非 CancelledError）——与原 _deals_loop 行为一致：记日志不外抛。
        """
        try:
            self._poll_deals()
        except Exception:  # noqa: BLE001
            logger.exception("deals loop unexpected error")

    # ---------------- 日终（E5/E6）----------------
    def _maybe_daily_close(self, now: Optional[datetime] = None) -> None:
        """本机时间 ≥ 14:30 且当日未算过 → 对每个组合调一次 update_daily。

        日终一次：日内盈亏 daily_pnl = 当前总市值 - prev_close（昨日收盘，update_peak 跨日刷新），
        检测 daily_loss 暂停 + 次日恢复。用上海时间（实盘固有时点，同 C6 1d 快照时点）。
        幂等：_last_daily_date 记录当日已算，避免每轮循环重复触发。
        """
        if now is None:
            now = now_shanghai()
        if (now.hour, now.minute) < _DAILY_CLOSE_TIME:
            return
        today = now.date()
        if self._last_daily_date == today:
            return
        self._last_daily_date = today
        for portfolio in self.portfolios:
            try:
                # 日终总市值：用组合最新现金 + 持仓市值（无最新 bar 时以当前持仓市值近似）
                total = portfolio.account.cash
                for ctx in portfolio.strategies:
                    for stock_code, pos in ctx.positions.items():
                        if pos.quantity == 0:
                            continue
                        # 用持仓成本计市值作为日终基准近似（无 bar close 时；有 bar 由 update_peak 已覆盖）
                        total += pos.avg_cost * pos.quantity
                was_paused = portfolio.risk_manager.daily_pause_active
                portfolio.risk_manager.update_daily(
                    total, today, portfolio.account.initial_capital
                )
                # B5：日内亏损熔断刚触发（daily_pause_active 变 True）→ 推送风控事件
                if portfolio.risk_manager.daily_pause_active and not was_paused:
                    self._emit("risk", {
                        "portfolio_id": portfolio.portfolio_id,
                        "rule": "daily_loss",
                        "triggered": True,
                        "message": "日内亏损熔断触发，当日暂停新开仓",
                    })
            except Exception:  # noqa: BLE001
                logger.exception(
                    "update_daily error (portfolio %s, date %s)",
                    portfolio.portfolio_id, today,
                )

    def _maybe_daily_bars(self, now: Optional[datetime] = None) -> None:
        """C6(B/C)：日终（≥14:30 当日一次）1d 快照 bar + 1w/1mon 通达信注入驱动。

        **14:30 数据源定案**：
          - 1d    → **iQuant 桥** /quote?period=1d（最新 forming 1d bar 的 OHLCV 快照）
          - 1w/1mon → **通达信 TQFormula.compute**（桥端 xtdata 拉不到 1w/1mon，仅此通路）
        与启动的去重/长度规则**一致**：
          - 1d 长度按 _code_period_count[(code,"1d")]（该股 1d 公式最大 formula_count，
            兜底 _period_count/全局），去重按 _sort_and_cap（stime 去重 + 截断）——同 _preheat；
          - 1w/1mon 走与 start() 相同的 _inject_startup_periods（count=-1 全量 + 取最新信号），
            日切 cache miss 时补注入——同启动注入。

        C6(B) 1d：拉桥 /quote?period=1d → _sort_and_cap → 构造 BarEvent(period="1d")
          → _fill_signal_cache 注入（period="1d"）→ on_bar 驱动 1d 策略。
        C6(C) 1w/1mon：信号预填 signal_cache[(sid, code, daily_time)]，此处驱动命中预填信号。
        幂等：_last_daily_bar_date 记录当日已触发。日切时（新 daily_time 的 1w/1mon
        cache miss）→ 通达信补注入。用本机 Asia/Shanghai 时钟（实盘固有时点，同 E5/E6）。
        """
        if now is None:
            now = now_shanghai()
        if (now.hour, now.minute) < _DAILY_CLOSE_TIME:
            return
        today = now.date()
        if self._last_daily_bar_date == today:
            return
        # 拉 1d 快照（供 1d 注入 + 构造 daily_time/stocks）。数据源 **iQuant 桥**
        # （1w/1mon 桥端 xtdata 拉不到，走通达信 _inject_startup_periods，见下 C6(C)）。
        # 长度/去重规则 **同启动预热（_preheat）**：
        #   长度 = 该股该周期最大 formula_count（_code_period_count[(code,"1d")]，
        #     兜底周期级 _period_count/全局 _formula_count）——非周期级统一值；
        #   去重 = _sort_and_cap 按 stime 排序 + 同 stime 去重 + 截断到 count 根。
        bars_by_code: Dict[str, list] = {}
        for code in self._bar_poller.stock_codes:
            count = self._code_period_count.get(
                (code, "1d"), self._period_count.get("1d", self._formula_count)
            )
            try:
                bars = self._dispatcher.query_quote(
                    code, period="1d", count=count
                )
            except BridgeUnavailableError:
                self._bridge_online = False
                logger.warning("bridge offline on daily bars (session %s)", self.session_id)
                return
            if bars:
                bars_by_code[code] = self._sort_and_cap(bars, count)
        if not bars_by_code:
            return
        # daily_time = 任一 code 最新 1d bar 的 stime（交易日 00:00）；解析失败用今日零点兜底
        daily_time = parse_bar_time(next(iter(bars_by_code.values()))[-1])
        if daily_time is None:
            daily_time = datetime.combine(today, datetime.min.time())
        self._last_daily_bar_date = today
        # 日切检测：新 daily_time 的 1w/1mon cache miss → 通达信补注入
        if self._startup_periods_missing(daily_time):
            self._inject_startup_periods(daily_time)
        # 1d 快照即最终值（14:30 后），每 code 取最新 forming 1d bar 的 OHLCV
        stocks = {code: to_ohlcv(bars[-1]) for code, bars in bars_by_code.items()}
        # C4(#28)：跨三周期共享去重缓存（key 含 period，1d/1w/1mon 互不干扰）
        df_cache: Dict = {}
        raw_cache: Dict = {}
        for period in ("1d", "1w", "1mon"):
            bar_event = BarEvent(stocks=stocks, bar_time=daily_time, period=period)
            for portfolio in self.portfolios:
                try:
                    self._handle_bar(
                        portfolio, bar_event, bars_by_code=bars_by_code,
                        df_cache=df_cache, raw_cache=raw_cache,
                    )
                except BridgeUnavailableError as e:
                    self._bridge_online = False
                    logger.warning(
                        "bridge unavailable on daily %s bar %s: %s",
                        period, daily_time, e,
                    )
                    return
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "daily %s bar error (portfolio %s, time %s)",
                        period, portfolio.portfolio_id, daily_time,
                    )

    # ---------------- bar 驱动 ----------------
    def _on_bar(self, bar: BarEvent) -> None:
        """BarPoller 回调（1m 节拍）：① 驱动 1m 策略；② 按 bar stime 边界分发长周期。

        边界判定只读 1m bar stime（periods_on_boundary），不引入本机时钟；
        5m/15m/30m/1h 策略在边界时点才被驱动（C6(A)），1m 节拍不再每 bar 算长周期。
        C4(#28)：df_cache/raw_cache 建在此（跨组合共享）——同 (code,period) 只 query_quote
        一次、同 (code,period,formula) 只 compute_injected 一次（TQ 计算最贵）。
        """
        df_cache: Dict = {}
        raw_cache: Dict = {}
        for portfolio in self.portfolios:
            try:
                self._handle_bar(portfolio, bar, df_cache=df_cache, raw_cache=raw_cache)
            except BridgeUnavailableError as e:
                # 下单时桥离线：标记并中断本 bar（循环下一轮心跳会暂停）
                self._bridge_online = False
                logger.warning("bridge unavailable on bar %s: %s", bar.bar_time, e)
                return
            except Exception:  # noqa: BLE001
                logger.exception(
                    "handle_bar error (portfolio %s, bar %s)",
                    portfolio.portfolio_id, bar.bar_time,
                )
        # C6(A)：1m bar 边界 → 分发 5m/15m/30m/1h（可累积）。
        # 同根 bar 只分发一次：慢股票下一轮 poll 才完成同一 stime（14:30 二次驱动），
        # 重复分发 = 周期全局重拉全股票 + 周期策略二次求值白费。分发成功后才记账
        # （桥异常中断时留口，慢股票再触发可重试）。
        if bar.bar_time not in self._dispatched_boundaries:
            for period in periods_on_boundary(bar.bar_time):
                try:
                    self._dispatch_period_bar(period, bar.bar_time)
                except BridgeUnavailableError as e:
                    self._bridge_online = False
                    logger.warning(
                        "bridge unavailable on %s boundary %s: %s", period, bar.bar_time, e
                    )
                    return
                except Exception:  # noqa: BLE001
                    logger.exception("dispatch %s boundary %s error", period, bar.bar_time)
            self._dispatched_boundaries.add(bar.bar_time)

    def _handle_bar(
        self,
        portfolio: Portfolio,
        bar: BarEvent,
        bars_by_code: Optional[Dict[str, list]] = None,
        df_cache: Optional[Dict] = None,
        raw_cache: Optional[Dict] = None,
    ) -> None:
        """一根 bar：盯回撤 → 取信号/风控 → 先落 submitted → 发单 → 等回填。

        复用回测 Portfolio.on_bar（信号优先级/风控/主从/熔断全复用），
        通过 self._engine（已注入 HttpBridgeDispatcher + LiveT1Checker）做实盘下单。
        切片5 时序（G1）：先写 LiveOrder(status=submitted)+commit，再发 passorder——
        崩在 passorder 已发、未确认窗口时 DB 至少有 submitted 记录，供 _poll_deals 挂回回填。
        submitted 阶段不 apply_trade、不写 LiveTrade，真实成交回报由 _poll_deals 回填确认后 apply。
        E5/E6：每 bar 先调 update_peak（跨日刷新 prev_close + 更新峰值 + max_drawdown 熔断检测）。
        """
        # E5：每 bar 更新峰值/回撤（分钟级 prev_close 只在跨日刷新，不误触 daily_loss）
        portfolio.risk_manager.update_peak(portfolio.total_value(bar), bar.bar_time.date())
        # H4：熔断计数持久化——update_peak 可能触发 max_drawdown（计数+1），计数变化才落库
        self._persist_breaker_count(portfolio)
        # F5：每 bar 刷一次桥可用持仓（多组合共享同一 bar 对象 → 强引用去重），
        # SELL 减仓上限用 m_nCanUseVolume（T+1 可用），供 cap_quantity 取数。
        if self._available_bar is not bar:
            self._available_bar = bar
            self._refresh_available_map()
        self._fill_signal_cache(
            portfolio, bar, bars_by_code=bars_by_code,
            df_cache=df_cache, raw_cache=raw_cache,
        )
        # C6：period 过滤——5m 边界 bar 不触发 1m 策略的风控单（_check_risks 读 bar.stocks close）
        orders = portfolio.on_bar(bar, signal_cache=self.signal_cache, period=bar.period)
        if not orders:
            return
        db = self._db_session_factory()
        try:
            for order in orders:
                ctx = portfolio.find_strategy(order.strategy_id)
                if ctx is None:
                    continue
                # B5：信号触发推送（在下单前——被 cap/拒单的信号也可见）
                self._emit("signal", {
                    "portfolio_id": portfolio.portfolio_id,
                    "strategy_id": order.strategy_id,
                    "stock_code": order.stock_code,
                    "signal_name": order.signal_name,
                    "signal_type": order.signal_type.value if order.signal_type else None,
                    "bar_time": order.bar_time.isoformat() if order.bar_time else None,
                })
                # BUY 首次建仓：确保 Position 存在（同回测 BacktestEngine；submitted 不 apply）
                pos = ctx.positions.get(order.stock_code)
                if pos is None and order.trade_type == TradeType.BUY:
                    pos = Position(order.stock_code)
                    ctx.positions[order.stock_code] = pos
                # F5/BUY 量上限：先定最终下单量（账户资金审批 / 桥 T+1 可用）再落
                # submitted——DB 下单量与实发一致，回填不误判 partial。None=不通过不下单。
                capped = self._engine.cap_quantity(order, portfolio.account, pos)
                if capped is None:
                    continue
                order.quantity = capped
                # ① 先写 submitted + commit（I4 命门窗口闭合）；计入在途集合（G7 计数）
                live_order = self._persist_order_submitted(db, order)
                self._pending_orders[live_order.id] = live_order
                # B5：订单状态推送（submitted——真实成交回报由 _poll_deals 回填后推 filled）
                self._emit("order", {
                    "portfolio_id": portfolio.portfolio_id,
                    "order_id": live_order.id,
                    "strategy_id": live_order.strategy_id,
                    "stock_code": live_order.stock_code,
                    "trade_type": live_order.trade_type,
                    "status": "submitted",
                    "quantity": live_order.quantity,
                    "price": float(live_order.price) if live_order.price is not None else None,
                    "bar_time": live_order.bar_time.isoformat() if live_order.bar_time else None,
                })
                db.commit()
                try:
                    # ② 再发 passorder（apply=False：submitted 阶段不更新账户持仓）
                    trade = self._engine.execute(order, portfolio.account, pos, apply=False)
                except BridgeUnavailableError:
                    # 桥离线：标 rejected + 置离线暂停（_on_bar 上层心跳循环据此暂停下单）
                    live_order.status = "rejected"
                    live_order.error_message = "bridge unavailable"
                    self._bridge_online = False
                    self._pending_orders.pop(live_order.id, None)
                    self._emit("order", {
                        "portfolio_id": portfolio.portfolio_id,
                        "order_id": live_order.id,
                        "status": "rejected",
                        "stock_code": live_order.stock_code,
                        "error_message": live_order.error_message,
                    })
                    db.commit()
                    continue
                except BridgeOrderRejected as e:
                    # 桥业务拒单（白名单/限额/重复）→ rejected，回显桥侧真实原因
                    # （此前被吞成笼统 "approval failed"，真机查不了原因）。
                    live_order.status = "rejected"
                    live_order.error_message = str(e)
                    self._pending_orders.pop(live_order.id, None)
                    self._emit("order", {
                        "portfolio_id": portfolio.portfolio_id,
                        "order_id": live_order.id,
                        "status": "rejected",
                        "stock_code": live_order.stock_code,
                        "error_message": live_order.error_message,
                    })
                    db.commit()
                    continue
                if trade is None:
                    # 兜底：execute 内部审批不过（前置 cap 与 execute 间账户变化）
                    # 或桥返回 {ok:false} 无 error（非 JSON 故障路径，#24）→ rejected
                    live_order.status = "rejected"
                    live_order.error_message = "approval failed"
                    self._pending_orders.pop(live_order.id, None)
                    self._emit("order", {
                        "portfolio_id": portfolio.portfolio_id,
                        "order_id": live_order.id,
                        "status": "rejected",
                        "stock_code": live_order.stock_code,
                        "error_message": live_order.error_message,
                    })
                    db.commit()
                    continue
                # ③ 桥受理成功：回写幂等 order_id，尝试同步定位 OrderRef（失败下轮回填再找）
                live_order.bridge_order_id = self._dispatcher.order_id(order)
                self._try_match_order_ref(live_order)
                # F6：SELL 已发单成功 → 从 bar 可用量扣减（同 bar 后续 SELL 见递减后的值，
                # 避免 A 卖 600+B 卖 400 超券商 available；扣过量钳到 0，券商端仍兜底）。
                if order.trade_type == TradeType.SELL:
                    self._t1_checker.consume_available(order.stock_code, order.quantity)
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _persist_breaker_count(self, portfolio: Portfolio) -> None:
        """H4：把组合 max_drawdown 累计触发次数持久化到 LiveSessionPortfolio.circuit_breaker_count。

        每 bar update_peak 后比对：计数未变则不落库（避免每 bar 写）；变化（熔断触发 / 达 3 次
        转手动）→ 写 count，达 3 次 status 转 circuit_broken（design §8.3）。找不到 link
        （组合未关联本 session）→ 跳过。写库失败不阻断交易，记日志。
        """
        count = portfolio.risk_manager.consecutive_drawdown_triggers
        old = self._breaker_count_written.get(portfolio.portfolio_id)
        if old == count:
            return
        # B5：计数递增（max_drawdown 熔断刚触发）→ 推送风控事件（首 bar old=None 不推）
        if old is not None and count > old:
            self._emit("risk", {
                "portfolio_id": portfolio.portfolio_id,
                "rule": "max_drawdown",
                "triggered": True,
                "count": count,
                "message": "最大回撤熔断触发（累计 %d 次）" % count,
            })
        db = self._db_session_factory()
        try:
            link = (
                db.query(LiveSessionPortfolio)
                .filter_by(
                    session_id=self.session_id,
                    portfolio_strategy_id=portfolio.portfolio_id,
                )
                .first()
            )
            if link is None:
                self._breaker_count_written[portfolio.portfolio_id] = count
                return
            link.circuit_breaker_count = count
            if count >= 3:
                link.status = "circuit_broken"
            db.commit()
            self._breaker_count_written[portfolio.portfolio_id] = count
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("persist breaker count error (portfolio %s)", portfolio.portfolio_id)
        finally:
            db.close()

    def _dispatch_period_bar(self, period: str, boundary_time: datetime) -> None:
        """C6(A) 边界分发：period 边界到（1m stime 判定）→ 拉该周期 bar → 注入 → 驱动该周期策略。

        对每 code 拉 query_quote(period, count=formula_count) 一次，既供公式注入又供 BarEvent。
        取每 code「最新已完成 bar」（stime < 本批 latest）的 OHLCV 构造 BarEvent
        （bar_time=boundary_time，与 1m 节拍对齐）——不用 forming 最新一根（未来函数）。
        桥拉取抛 BridgeUnavailableError → 向上传播由 _on_bar 置离线。无完成 bar 的 code 跳过。

        周期 guard：periods_on_boundary 是纯算术（minute%15==0 等），不查实例有无该周期策略，
        会带出实例无人用的周期（如 14:30 的 15m）。此处按 _strategy_periods 过滤——实例无该
        周期策略直接 return，避免白拉（拉完 period 过滤全跳过，不出单纯浪费）。
        """
        if period not in self._strategy_periods:
            return
        bars_by_code: Dict[str, list] = {}
        stocks: Dict[str, dict] = {}
        # C4(#28)：预拉 count = 该周期策略最大 formula_count（够最长公式，注入不欠历史）
        count = self._period_count.get(period, self._formula_count)
        for code in self._bar_poller.stock_codes:
            # 按 (code, period) 取该股票该周期所需根数（比全局 _period_count 更细，按需）。
            # 走预热缓存 + 增量拼接（省去每边界全量重拉 count 根）。
            cp_count = self._code_period_count.get((code, period), count)
            bars = self._get_bars_with_increment(code, period, cp_count)
            if not bars:
                continue
            bars_by_code[code] = bars
            cb = latest_completed_bar(bars)
            if cb is None:
                continue
            stocks[code] = to_ohlcv(cb)
        if not stocks:
            return
        bar_event = BarEvent(stocks=stocks, bar_time=boundary_time, period=period)
        df_cache: Dict = {}
        raw_cache: Dict = {}
        for portfolio in self.portfolios:
            self._handle_bar(
                portfolio, bar_event, bars_by_code=bars_by_code,
                df_cache=df_cache, raw_cache=raw_cache,
            )

    # ---------------- F5：桥可用持仓（SELL 减仓上限）----------------
    def _refresh_available_map(self) -> None:
        """F5：拉桥 /positions，按 code(instrument.exchange) 聚合 m_nCanUseVolume。

        桥无该仓/拉取失败（离线）→ 空表 → get_available_shares 全量放行（券商端
        T+1 兜底，避免误伤正常卖出；G6 处理券商拒单）。
        """
        try:
            rows = self._dispatcher.query_positions()
        except BridgeUnavailableError:
            self._t1_checker.set_available_map({})
            return
        m: Dict[str, int] = {}
        for r in rows or []:
            inst = r.get("instrument")
            exch = r.get("exchange")
            avail = r.get("available")
            if inst and exch and avail is not None:
                m["%s.%s" % (inst, exch)] = int(avail)
        self._t1_checker.set_available_map(m)

    # ---------------- 公式信号注入（0010 + C4 #28 三维去重）----------------
    def _fill_signal_cache(
        self,
        portfolio: Portfolio,
        bar: BarEvent,
        bars_by_code: Optional[Dict[str, list]] = None,
        df_cache: Optional[Dict] = None,
        raw_cache: Optional[Dict] = None,
    ) -> None:
        """实盘逐 bar 算公式信号填 signal_cache。预填模式（不改 Portfolio）。

        对每个策略 × bar.stocks 每只股票：
          bridge query_quote(code, period, count=N) 拉历史+实时 bar
          → _bars_to_formula_df 转 OHLCV DataFrame
          → TQFormula.compute_injected 内存注入算公式
          → _extract_latest_signal 取最后一条（当前 bar 信号）
          → 填 signal_cache[(strategy_id, code, bar.bar_time)]
        C6 节拍过滤：bar.period 非 None 时只注入匹配周期的策略（5m 边界 bar 不注入 1m 策略）；
        1w/1mon（_STARTUP_ONLY_PERIODS）走通达信启动/日终注入，不拉桥。
        bars_by_code：调用方已预拉好的 bars（边界/日终分发），避免二次拉桥。
        C4(#28) 三维去重（单 bar 生命周期，跨组合共享）：
          df_cache[(code, period)]   → 同 key 只 query_quote 一次（count 更大时升级重拉）
          raw_cache[(code,period,formula)] → 同 key 只 compute_injected 一次（TQ 计算最贵）
          count 不进 key 的前提：count 是 Formula.formula_count 公式级字段（#27），
          同公式 count 恒定 → 同 (code,period,formula) 的 count 必然相同。
        signal_cache key 仍带 strategy_id（隔离不变，值相同各自存一份）。
        无 tq_formula / 策略无公式映射 / 拉取为空 / 算失败 → 跳过（该股该 bar 无公式信号）。
        """
        if self._tq_formula is None or not self._formula_by_strategy:
            return
        if df_cache is None:
            df_cache = {}
        if raw_cache is None:
            raw_cache = {}
        for ctx in portfolio.strategies:
            formula_name = self._formula_by_strategy.get(ctx.strategy_id)
            if not formula_name:
                continue
            # C6：该 bar 只注入匹配周期的策略
            if bar.period is not None and ctx.period != bar.period:
                continue
            # C6(C)：1w/1mon 走通达信启动/日终注入，不拉桥
            if ctx.period in _STARTUP_ONLY_PERIODS:
                continue
            period = ctx.period
            # #27→#28：注入 count 来自 Formula.formula_count（公式级），非全局 200
            count = self._formula_count_by_name.get(formula_name, self._formula_count)
            for code in bar.stocks:
                # 股票池过滤：池外股票不拉公式（多组合共享行情 bar，各策略只算自己池内）
                if ctx.stock_pool is not None and code not in ctx.stock_pool:
                    continue
                try:
                    bars = self._fetch_cached_bars(
                        df_cache, bars_by_code, code, period, count
                    )
                except BridgeUnavailableError:
                    # 拉历史失败：跳过该股（不阻断 on_bar，风控信号仍可触发）
                    logger.warning("quote failed for formula inject %s %s", code, period)
                    continue
                raw_key = (code, period, formula_name)
                if raw_key not in raw_cache:
                    df = self._bars_to_formula_df(bars, code)
                    raw = None
                    if df is not None:
                        raw = self._tq_formula.compute_injected(
                            formula_name=formula_name, ohlcv_df=df,
                            stocks=[code], period=period,
                        )
                    raw_cache[raw_key] = self._extract_latest_signal(raw, code)
                outputs = raw_cache[raw_key]
                if outputs:
                    self.signal_cache[(ctx.strategy_id, code, bar.bar_time)] = outputs

    def _fetch_cached_bars(
        self,
        df_cache: Dict,
        bars_by_code: Optional[Dict[str, list]],
        code: str,
        period: str,
        count: int,
    ) -> list:
        """拉取去重：df_cache[(code, period)] 同 key 只实际拉取一次（单 bar 生命周期）。

        缓存值 (bars, used_count)。同 code+period 的公式 count 更大 → 升级重拉（过小会缺
        历史，公式长均线 NaN 静默失效）；bars_by_code 已按该周期最大 count 预拉 → 直接复用，
        记 used=(code,period) 最大 count，避免无谓升级重拉。
        底层实际拉取走 _get_bars_with_increment（预热缓存 + 增量拼接），不再直接 query_quote
        全量——1m 算公式（bars_by_code=None）与升级重拉都受益。
        """
        key = (code, period)
        cached = df_cache.get(key)
        if cached is not None:
            bars, used = cached
            if count <= used:
                return bars
            bars = self._get_bars_with_increment(code, period, count)
            df_cache[key] = (bars, count)
            return bars
        if bars_by_code is not None and code in bars_by_code:
            bars = bars_by_code[code]
            df_cache[key] = (bars, max(count, self._code_period_count.get((code, period),
                                                                          self._formula_count)))
            return bars
        bars = self._get_bars_with_increment(code, period, count)
        df_cache[key] = (bars, count)
        return bars

    @staticmethod
    def _bars_to_formula_df(bars: list, code: str) -> Optional[dict]:
        """桥 bar dict 列表 → {Amount/Volume/Close/Open/High/Low: pandas.DataFrame}。

        桥 bar 字段：stime(yyyymmddHHMMSS)/time(时间戳)/index(历史工具) + 小写 OHLCV。
        时间统一用 parse_bar_time（与 BarPoller 同规则），兼容 stime/time/index 各来源。
        输出：每字段单列 DataFrame（列=[code]，行=DatetimeIndex）。
        空 bars / 无有效时间 → None（调用方跳过）。
        """
        if not bars:
            return None
        # pandas 在此函数内首次按需 import（非顶部）：pandas 较重，且本函数仅在
        # 公式注入路径调用；避免模块导入期无条件加载（审计 #31：math 已提顶，pandas 刻意保留 lazy）。
        import pandas as pd

        times, o, h, l, c, v, a = [], [], [], [], [], [], []
        for b in bars:
            t = parse_bar_time(b)
            if t is None:
                continue

            def _num(key):
                val = b.get(key)
                if val is None or val == "":
                    return 0.0
                try:
                    return float(val)
                except (TypeError, ValueError):
                    return 0.0

            times.append(t)
            o.append(_num("open"))
            h.append(_num("high"))
            l.append(_num("low"))
            c.append(_num("close"))
            v.append(int(_num("volume")))
            a.append(_num("amount"))
        if not times:
            return None
        idx = pd.DatetimeIndex(times)
        return {
            "Open": pd.DataFrame({"open": o}, index=idx).rename(columns={"open": code}),
            "High": pd.DataFrame({"high": h}, index=idx).rename(columns={"high": code}),
            "Low": pd.DataFrame({"low": l}, index=idx).rename(columns={"low": code}),
            "Close": pd.DataFrame({"close": c}, index=idx).rename(columns={"close": code}),
            "Volume": pd.DataFrame({"volume": v}, index=idx).rename(columns={"volume": code}),
            "Amount": pd.DataFrame({"amount": a}, index=idx).rename(columns={"amount": code}),
        }

    @staticmethod
    def _extract_latest_signal(raw: Optional[dict], code: str) -> List[dict]:
        """从 formula_process_mul_zb 返回取最后一条 bar 的信号 → [{"name", "value"}]。

        raw: {stock_code: {var_name: [{"Date","Value"}, ...]}, "ErrorId", ...}
        实盘逐 bar 算，注入 N 根算出 N 条输出，取最后一条即当前 bar 信号
        （避开回测的索引对齐全段逻辑）。ErrorId 非 0/19 → 空。
        """
        if not isinstance(raw, dict) or not raw:
            return []
        err = raw.get("ErrorId")
        if err is not None and str(err) not in ("0", "19"):
            return []
        stock_data = raw.get(code)
        if not isinstance(stock_data, dict) or not stock_data:
            return []
        outputs: List[dict] = []
        for var_name, val_list in stock_data.items():
            if var_name in _FORMULA_META_KEYS:
                continue
            if not isinstance(val_list, list) or not val_list:
                continue
            last = val_list[-1]
            if not isinstance(last, dict):
                continue
            v = last.get("Value")
            if v is None:
                continue
            outputs.append({"name": var_name, "value": _to_int(v)})
        return outputs

    def _inject_startup_periods(self, daily_time: datetime) -> None:
        """C6(C)：1w/1mon 策略通达信注入——TQFormula.compute 自取历史 → 最新信号填 signal_cache。

        key=(strategy_id, stock_code, daily_time)，与日终 _maybe_daily_bars 驱动用的
        bar_time 一致，驱动时命中预填信号。桥端 xtdata 拉不到 1w/1mon，通达信是唯一通路。
        start() 启动调一次；_maybe_daily_bars 检测日切 cache miss 时补调。
        单策略/单股 compute 失败 → 跳过（不阻断其余）。
        """
        if self._tq_formula is None or not self._formula_by_strategy:
            return
        codes = list(self._bar_poller.stock_codes)
        for portfolio in self.portfolios:
            for ctx in portfolio.strategies:
                if ctx.period not in _STARTUP_ONLY_PERIODS:
                    continue
                formula_name = self._formula_by_strategy.get(ctx.strategy_id)
                if not formula_name:
                    continue
                for code in codes:
                    try:
                        raw = self._tq_formula.compute(
                            formula_name, "", [code], period=ctx.period, count=-1
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "startup period compute failed %s %s", ctx.period, code
                        )
                        continue
                    outputs = self._extract_latest_signal(raw, code)
                    if outputs:
                        self.signal_cache[(ctx.strategy_id, code, daily_time)] = outputs

    def _startup_periods_missing(self, daily_time: datetime) -> bool:
        """1w/1mon 策略在 daily_time 的信号是否全部已预填（cache miss → 需补注入）。

        _maybe_daily_bars 日切检测用：新交易日的 daily_time 尚无 cache 键 → True。
        """
        for portfolio in self.portfolios:
            for ctx in portfolio.strategies:
                if ctx.period not in _STARTUP_ONLY_PERIODS:
                    continue
                for code in self._bar_poller.stock_codes:
                    if (ctx.strategy_id, code, daily_time) not in self.signal_cache:
                        return True
        return False

    # ---------------- 订单状态机 + 成交回报回填（切片5）----------------
    def _persist_order_submitted(self, db: Session, order: OrderEvent) -> LiveOrder:
        """写 LiveOrder(status=submitted)，不写 LiveTrade（回填确认成交才写）。

        提交时序（G1/I4）：_handle_bar 先调本方法 + commit，再发 passorder——
        崩在 passorder 已发、未确认窗口时 DB 至少有 submitted 记录供挂回。
        """
        live_order = LiveOrder(
            live_session_id=self.session_id,
            portfolio_strategy_id=order.portfolio_id,
            strategy_id=order.strategy_id,
            stock_code=order.stock_code,
            trade_type=order.trade_type.value.lower(),  # "buy"/"sell"
            order_type="limit",
            price=order.price,
            quantity=order.quantity,
            filled_quantity=0,
            filled_price=None,
            status="submitted",
            signal_name=order.signal_name or None,
            signal_type=order.signal_type.value if order.signal_type else None,
            bar_time=order.bar_time,
        )
        db.add(live_order)
        db.flush()  # 取 live_order.id
        return live_order

    def _try_match_order_ref(self, live_order: LiveOrder) -> None:
        """轮询桥 /orders 定位本单的 m_strOrderRef 回写（G3 匹配键）。

        passorder 返回 0 无法预知 OrderRef，故按组合键从桥 /orders 列表定位：
        source=BRIDGE（排除 GUI 手动单）+ instrument/exchange 拼接 = 股票代码
        + direction（48买/49卖）+ volume（委托量）。全局限 1 session 串行，
        同股票同向同量的在途单唯一，不会撞单。
        找不到 → order_ref 留 None，下轮 _poll_deals 再找。
        """
        try:
            orders = self._dispatcher.query_orders()
        except BridgeUnavailableError:
            return  # 桥离线，下轮再找
        code = live_order.stock_code
        op_dir = 48 if live_order.trade_type == "buy" else 49
        for o in orders or []:
            if o.get("source") != "BRIDGE":
                continue
            inst = o.get("instrument") or ""
            exch = o.get("exchange") or ""
            if "%s.%s" % (inst, exch) != code:
                continue
            if o.get("direction") != op_dir:
                continue
            if o.get("volume") != live_order.quantity:
                continue
            live_order.order_ref = o.get("order_ref")
            return

    def _poll_deals(self) -> None:
        """主循环每轮：查未完结 LiveOrder → 定位 OrderRef → 轮询桥 /deals → 回填（G2）。

        每轮处理：未回填 order_ref 的单先尝试定位；有 order_ref 的单按 order_ref
        过滤 /deals 成交回报，调 _backfill_order 更新状态 + 写 LiveTrade + apply。
        桥离线/查失败 → 本轮跳过（不改状态），下轮重试。
        """
        db = self._db_session_factory()
        try:
            pending = (
                db.query(LiveOrder)
                .filter(
                    LiveOrder.live_session_id == self.session_id,
                    LiveOrder.status.in_(["submitted", "partial"]),
                )
                .all()
            )
            if not pending:
                self._sync_pending_orders(db)  # 无在途单 → 清空计数（G7）
                return
            # 1. 未回填 order_ref 的单：尝试定位
            for lo in pending:
                if lo.order_ref is None:
                    self._try_match_order_ref(lo)
            db.commit()
            # 2. 有 order_ref 的单：查 /deals 回填
            need_ref = [lo for lo in pending if lo.order_ref is not None]
            if not need_ref:
                self._sync_pending_orders(db)
                return
            try:
                deals = self._dispatcher.query_deals()
            except BridgeUnavailableError:
                return  # 桥离线，本轮跳过
            for lo in need_ref:
                matched = [d for d in deals if d.get("order_ref") == lo.order_ref]
                if not matched:
                    continue
                self._backfill_order(db, lo, matched)
            db.commit()
            # 3. G7：重查剩余 submitted/partial 同步在途集合（filled/rejected 自然移除）
            self._sync_pending_orders(db)
        finally:
            # 异常向上抛到 _deals_loop 统一记日志（不再此处静默吞），
            # rollback 清理未提交事务，close 归还连接。
            db.rollback()
            db.close()

    def _sync_pending_orders(self, db: Session) -> None:
        """重查 DB 剩余 submitted/partial 同步在途集合（G7 计数）。

        以 DB 为准：回填置 filled / 拒单置 rejected 的单自然移除，_handle_bar 新增
        的单（已 commit）自然纳入。调用点在 _poll_deals 各出口，保证 get_session
        读到的是最近一轮的实际在途单数。
        """
        remaining = (
            db.query(LiveOrder)
            .filter(
                LiveOrder.live_session_id == self.session_id,
                LiveOrder.status.in_(["submitted", "partial"]),
            )
            .all()
        )
        self._pending_orders = {lo.id: lo for lo in remaining}

    def _backfill_order(self, db: Session, live_order: LiveOrder, matched_deals: list) -> None:
        """据成交回报回填 LiveOrder + LiveTrade + apply_trade（G2/G6）。

        聚合 order_ref 下全部成交：总成交量/总金额/总佣金，成交均价 = 金额/量。
        filled（成交量 ≥ 委托量）→ status=filled + apply_trade（首次用真实价/量/佣金）；
        partial（成交量 < 委托量）→ status=partial，写/更新 LiveTrade 但不 apply
        （等最终 filled 或撤单，避免部分成交误动持仓）。
        """
        total_qty = sum(int(d.get("volume") or 0) for d in matched_deals)
        if total_qty <= 0:
            return  # 无实际成交（可能已撤），下轮重查
        total_amount = sum(Decimal(str(d.get("amount") or 0)) for d in matched_deals)
        total_commission = sum(Decimal(str(d.get("commission") or 0)) for d in matched_deals)
        avg_price = total_amount / Decimal(total_qty)

        live_order.filled_quantity = total_qty
        live_order.filled_price = avg_price
        live_order.status = "filled" if total_qty >= live_order.quantity else "partial"
        self._last_backfill_time = now_shanghai()  # G7：记录最近一次回填时点
        # B5：订单状态推送（filled/partial 都由成交回报回填推进）
        self._emit("order", {
            "portfolio_id": live_order.portfolio_strategy_id,
            "order_id": live_order.id,
            "status": live_order.status,
            "stock_code": live_order.stock_code,
            "filled_quantity": live_order.filled_quantity,
            "filled_price": float(avg_price),
        })

        # 写/更新 LiveTrade（按 order_ref 聚合为一笔）
        trade_time = self._parse_trade_time(matched_deals[-1])
        existing = (
            db.query(LiveTrade)
            .filter(LiveTrade.live_order_id == live_order.id)
            .first()
        )
        if existing:
            existing.price = avg_price
            existing.quantity = total_qty
            existing.amount = total_amount
            existing.commission = total_commission
            existing.trade_time = trade_time
            trade_rec = existing
        else:
            trade_rec = LiveTrade(
                live_session_id=self.session_id,
                live_order_id=live_order.id,
                portfolio_strategy_id=live_order.portfolio_strategy_id,
                strategy_id=live_order.strategy_id,
                stock_code=live_order.stock_code,
                trade_type=live_order.trade_type,
                price=avg_price,
                quantity=total_qty,
                amount=total_amount,
                commission=total_commission,
                stamp_duty=Decimal("0"),  # 首期 0：DEAL 印花税字段待真机验证
                trade_time=trade_time,
            )
            db.add(trade_rec)
        db.flush()  # 取 trade_rec.id（B5 trade 事件用）
        # B5：成交回报推送
        self._emit("trade", {
            "portfolio_id": live_order.portfolio_strategy_id,
            "trade_id": trade_rec.id,
            "stock_code": trade_rec.stock_code,
            "trade_type": trade_rec.trade_type,
            "price": float(avg_price),
            "quantity": total_qty,
            "amount": float(total_amount),
        })

        # filled：回填确认后 apply_trade（submitted 阶段未 apply，此处首次落持仓）
        if live_order.status == "filled":
            self._apply_filled_trade(
                live_order, avg_price, total_qty, total_amount, total_commission
            )

    @staticmethod
    def _parse_trade_time(deal: dict) -> datetime:
        """DEAL 的 trade_date(YYYYMMDD) + trade_time(HHMMSS / HH:MM:SS) → datetime。

        桥 query_deals 返回 m_strTradeTime/m_strTradeDate 原文；解析失败用 now() 兜底。
        """
        d = str(deal.get("trade_date") or "").strip()
        t = str(deal.get("trade_time") or "").strip()
        try:
            if len(t) == 6 and t.isdigit():
                return datetime.strptime(d + t, "%Y%m%d%H%M%S")
            if ":" in t:
                return datetime.strptime(d + " " + t, "%Y%m%d %H:%M:%S")
        except (ValueError, TypeError):
            pass
        return now_shanghai()

    def _apply_filled_trade(self, live_order: LiveOrder, price: Decimal,
                            qty: int, amount: Decimal, commission: Decimal) -> None:
        """回填确认 filled 后 apply_trade 更新虚拟持仓/现金（G6）。

        仅在此处 apply——submitted 阶段不 apply，真实成交回报确认后才动虚拟账户。
        signal_type 从 LiveOrder 取（Position.apply_trade 据此判 ADD）。
        """
        portfolio = next(
            (p for p in self.portfolios if p.portfolio_id == live_order.portfolio_strategy_id),
            None,
        )
        if portfolio is None:
            return
        ctx = portfolio.find_strategy(live_order.strategy_id)
        if ctx is None:
            return
        pos = ctx.positions.get(live_order.stock_code)
        sig_type = SignalType(live_order.signal_type) if live_order.signal_type else None
        trade = TradeEvent(
            strategy_id=live_order.strategy_id,
            portfolio_id=live_order.portfolio_strategy_id,
            stock_code=live_order.stock_code,
            trade_type=TradeType(live_order.trade_type.upper()),
            price=price,
            quantity=qty,
            amount=amount,
            commission=commission,
            stamp_duty=Decimal("0"),
            trade_time=live_order.bar_time or now_shanghai(),
            signal_type=sig_type,
        )
        if pos is None and trade.trade_type == TradeType.BUY:
            pos = Position(live_order.stock_code)
            ctx.positions[live_order.stock_code] = pos
        portfolio.account.apply_trade(trade)
        if pos is not None:
            pos.apply_trade(trade)
            # B5：持仓变化推送（filled 后真实持仓/成本；pnl 无市价标记暂为 0）
            self._emit("position", {
                "portfolio_id": live_order.portfolio_strategy_id,
                "stock_code": live_order.stock_code,
                "quantity": pos.quantity,
                "avg_cost": float(pos.avg_cost),
                "market_value": float(pos.avg_cost * pos.quantity),
                "pnl": 0,
            })

    # ---------------- 持仓恢复 ----------------
    def recover(self, db: Session) -> None:
        """Core 重启后从 live_trades 重放，重建各组合虚拟持仓/虚拟现金。

        按 trade_time 顺序重放本 session 全部 live_trades：BUY 累加持仓+扣现金，
        SELL 减持仓+加现金。signal_type 从关联 LiveOrder 取（LiveTrade 无此列），
        用于 Position.apply_trade 区分 ADD（影响 add_count）。虚拟现金以成本计（§93）。
        """
        ports_by_id = {p.portfolio_id: p for p in self.portfolios}
        trades = (
            db.query(LiveTrade)
            .filter(LiveTrade.live_session_id == self.session_id)
            .order_by(LiveTrade.trade_time, LiveTrade.id)
            .all()
        )
        # 预取关联 LiveOrder 的 signal_type（LiveTrade 无 signal_type 列）
        order_ids = {t.live_order_id for t in trades if t.live_order_id is not None}
        sig_type_by_order: Dict[int, Optional[str]] = {}
        if order_ids:
            for lo in db.query(LiveOrder).filter(LiveOrder.id.in_(order_ids)).all():
                sig_type_by_order[lo.id] = lo.signal_type
        for tr in trades:
            port = ports_by_id.get(tr.portfolio_strategy_id)
            if port is None:
                continue
            ctx = port.find_strategy(tr.strategy_id)
            if ctx is None:
                continue
            sig_type_str = sig_type_by_order.get(tr.live_order_id)
            trade = TradeEvent(
                strategy_id=tr.strategy_id,
                portfolio_id=tr.portfolio_strategy_id,
                stock_code=tr.stock_code,
                trade_type=TradeType(tr.trade_type.upper()),
                price=Decimal(str(tr.price)),
                quantity=tr.quantity,
                amount=Decimal(str(tr.amount)),
                commission=Decimal(str(tr.commission)),
                stamp_duty=Decimal(str(tr.stamp_duty)),
                trade_time=tr.trade_time,
                signal_type=SignalType(sig_type_str) if sig_type_str else None,
            )
            pos = ctx.positions.get(tr.stock_code)
            if pos is None:
                pos = Position(tr.stock_code)
                ctx.positions[tr.stock_code] = pos
            port.account.apply_trade(trade)
            pos.apply_trade(trade)
        # 切片5 I4：挂回未完结 LiveOrder（submitted/partial）供主循环 _poll_deals 回填。
        # 跨重启场景：passorder 已发券商、Core 崩在落库前 → DB 无 live_trades 但有
        # submitted 记录，挂回后 _poll_deals 按 OrderRef 匹配真实成交补记（G2/G6）。
        pending = (
            db.query(LiveOrder)
            .filter(
                LiveOrder.live_session_id == self.session_id,
                LiveOrder.status.in_(["submitted", "partial"]),
            )
            .all()
        )
        self._pending_orders = {lo.id: lo for lo in pending}
        if pending:
            logger.info(
                "recover: %d pending live orders to backfill (session %s)",
                len(pending), self.session_id,
            )
        # D4：读回熔断计数（LiveSessionPortfolio.circuit_breaker_count）——重启后累计次数不丢。
        # 达 3 次 → 转手动恢复（manual_recovery + circuit_breaker_active=True 停新开仓等待人工，
        # 同 status=circuit_broken 语义）；<3 次的单日熔断当天已恢复，重启不补挂（单一计数模型，
        # 可接受）。预置 _breaker_count_written 避免首 bar 重复落库。
        links = (
            db.query(LiveSessionPortfolio)
            .filter(LiveSessionPortfolio.session_id == self.session_id)
            .all()
        )
        for link in links:
            port = ports_by_id.get(link.portfolio_strategy_id)
            if port is None or not link.circuit_breaker_count:
                continue
            port.risk_manager.consecutive_drawdown_triggers = link.circuit_breaker_count
            self._breaker_count_written[link.portfolio_strategy_id] = link.circuit_breaker_count
            if link.circuit_breaker_count >= 3:
                port.risk_manager.manual_recovery = True
                port.risk_manager.circuit_breaker_active = True
        # D3：虚拟持仓 vs 桥实际 /positions 对账（仅告警不修正，见 _reconcile_positions）
        self._reconcile_positions()

    def _reconcile_positions(self) -> None:
        """D3：对账——虚拟持仓(按 code 聚合) vs 桥实际 /positions，不一致仅记录+告警。

        recover 重建的虚拟持仓以 live_trades 为唯一源；桥 /positions 是账户真实持仓。
        比对口径：按 stock_code 聚合所有组合策略虚拟净持仓 vs 桥 volume。
        差异只记 `_reconcile_mismatches` + 告警日志，**不自动修正账面**（首期安全：
        在途单未回填/桥行情延迟期可能假不一致，自动修正反而引入错账；真机跑顺后
        如需自动校准再放开）。桥离线 → 记日志跳过（不阻断 start）。
        附加提示：实际有仓但虚拟无仓（如 Core 宕机期手动下单）也记录，供人工核对。
        """
        self._reconcile_mismatches = []
        try:
            rows = self._dispatcher.query_positions()
        except BridgeUnavailableError:
            logger.warning("reconcile skipped: bridge offline (session %s)", self.session_id)
            return
        real: Dict[str, int] = {}
        for r in rows or []:
            inst = r.get("instrument")
            exch = r.get("exchange")
            vol = r.get("volume")
            if inst and exch and vol is not None:
                real["%s.%s" % (inst, exch)] = int(vol)
        virtual: Dict[str, int] = {}
        for port in self.portfolios:
            for ctx in port.strategies:
                for code, pos in ctx.positions.items():
                    if pos.quantity == 0:
                        continue
                    virtual[code] = virtual.get(code, 0) + pos.quantity
        for code in sorted(set(real) | set(virtual)):
            v = virtual.get(code, 0)
            r = real.get(code, 0)
            if v == r:
                continue
            self._reconcile_mismatches.append(
                {"code": code, "virtual": v, "real": r, "diff": v - r}
            )
            logger.warning(
                "reconcile mismatch (session %s) %s: virtual=%d real=%d diff=%d",
                self.session_id, code, v, r, v - r,
            )

    @property
    def bridge_online(self) -> bool:
        """桥是否在线（心跳/下单成功为 True，离线暂停期间为 False）。"""
        return self._bridge_online

    @property
    def pending_orders_count(self) -> int:
        """在途未完结单数（submitted/partial），供 session API 桥状态并入（G7）。"""
        return len(self._pending_orders)

    @property
    def last_backfill_time(self) -> Optional[datetime]:
        """最近一次 /deals 成交回报回填时点（None=尚无回填）。"""
        return self._last_backfill_time

    @property
    def dispatcher(self) -> HttpBridgeDispatcher:
        """暴露 dispatcher 供桥状态查询等只读访问。"""
        return self._dispatcher
