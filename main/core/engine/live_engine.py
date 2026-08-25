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
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
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
from .trading_calendar import TradingCalendar
# 时间/周期/数值工具归 live 协作者子包（core.engine.live.timing，0010 步骤 0）；
# re-export 其中被测试直接 import 的符号（now_shanghai/periods_on_boundary/_CST），
# 保持 core.engine.live_engine 命名空间可见。
from .live.timing import (
    _CST,
    _parse_insert_utc,
    now_shanghai,
    periods_on_boundary,
)
from .live.event_bus import EventBus
from .live.context import EngineContext
from .live.market_data import (
    MarketDataService,
    _FORMULA_META_KEYS,
    _STARTUP_ONLY_PERIODS,
)
from .live.order_machine import (
    OrderStateMachine,
    _ORDER_REF_MATCH_TIMEOUT,
    _ORDER_INSERT_TOLERANCE,
    _ORDER_STATUS_FILLED,
    _ORDER_STATUS_PARTIAL_CANCELED,
    _ORDER_STATUS_CANCELED,
    _ORDER_STATUS_JUNK,
    _ORDER_STATUS_TERMINAL_CANCELED,
    _ORDER_STATUS_TERMINAL_REJECTED,
)
from core.models import LiveOrder, LiveTrade, LiveSessionPortfolio
from core.tq.formula import TQFormula
from tq_iquant_shared.constants import SignalType, TradeType

logger = logging.getLogger(__name__)

# 实盘日终判定时点（E5/E6 update_daily + C6(B/C) 1d 快照/1w/1mon 注入共用）。
# 上海 14:30 收盘后驱动。提为常量消除魔法数字（审计 #33）。
_DAILY_CLOSE_TIME = (14, 30)

# 深交所收盘时点（上海 15:00）。收盘后禁止新下单——报不进交易所，会成待报单
# 冻结持仓永不成交（真机 2026-08-14 id40-41，bar_time=15:02:42）。
# 用 bar.bar_time 判定（非墙钟：不依赖本机时钟/时区）。仅拦新落单，信号仍推。
_MARKET_CLOSE_TIME = (15, 0)
# 收盘清扫时点（15:05）：交易所已收盘、iQuant 自动撤单已落定后，对当日仍卡在
# submitted/partial 的单做一次确定性兜底——成交回填 + 终态同步 + 剩余未成交按 A 股
# 规则（收盘未成交=撤单）标 canceled。晚 5 分钟给柜台/桥状态收敛留时间。
_MARKET_CLOSE_SWEEP_TIME = (15, 5)

# 委托状态机/成交回填相关常量（_ORDER_REF_MATCH_TIMEOUT 等）与 OrderStateMachine 一并
# 迁入 core.engine.live.order_machine（0010 步骤 3），此处由顶部 import re-export，保持
# core.engine.live_engine 命名空间可见。


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
        trading_calendar: Optional[TradingCalendar] = None,
    ):
        # 交易日历（下单总闸）：默认桥 xtdata 权威日历，桥离线时 TradingCalendar
        # fail-open（工作日放行），测试可注入假日历。
        self._calendar = trading_calendar or TradingCalendar(dispatcher.query_calendar)
        self._bar_poller = bar_poller
        # 协作者共享状态容器（0010 步骤 2a）：session_id/portfolios/dispatcher/
        # db_session_factory/code_period_count/clock 归 EngineContext，引擎上的同名
        # 属性保留为 property 委托（见类体下方），调用点与测试直连无需改动。
        self.ctx = EngineContext(
            session_id=session_id,
            portfolios=portfolios,
            dispatcher=dispatcher,
            db_session_factory=db_session_factory,
            code_period_count=code_period_count,
            clock=now_shanghai,
        )
        self._poll_interval = poll_interval
        # G5：/deals 成交回报轮询独立节拍（默认 5s，比主循环 60s 更短）——成交秒级回报，
        # 持仓/资金反馈要近实时，不跟 bar 拉取同频。
        self._deals_poll_interval = deals_poll_interval
        # 实盘执行引擎：复用回测 ExecutionEngine，注入桥 dispatcher + 实盘 T+1 检查
        # F5：t1_checker 持每 bar 刷新的桥可用表（SELL 减仓上限用 m_nCanUseVolume）
        self._t1_checker = LiveT1Checker()
        self._engine = ExecutionEngine(dispatcher, self._t1_checker)

        # 行情/信号协作者（0010 步骤 2b）：预热/增量 bar 缓存、公式信号注入、周期边界
        # 分发、1w/1mon 通达信注入归 MarketDataService。on_bar 回调注入 self._handle_bar
        # （服务不反向 import LiveEngine，周期/日终 bar 经此回调交回引擎下单/风控）；
        # F5 可用持仓写回经 set_available_map 窄回调。引擎保留同名薄委托方法/property，
        # 既有调用点与测试直连（engine.signal_cache={...}、engine._preheat() 等）穿透。
        self.market_data = MarketDataService(
            self.ctx,
            bar_poller,
            on_bar=self._handle_bar,
            set_available_map=self._t1_checker.set_available_map,
            tq_formula=tq_formula,
            formula_by_strategy=formula_by_strategy,
            formula_count=formula_count,
            formula_count_by_name=formula_count_by_name,
        )
        # 委托状态机/成交回填协作者（0010 步骤 3）：order_ref 匹配、/deals 轮询回填、
        # 终态同步、陈旧单失效、filled apply 归 OrderStateMachine。持有 _pending_orders/
        # _last_backfill_time。事件发射经 self._emit 回调注入（不反向 import LiveEngine）。
        # 引擎保留全部同名方法/属性为薄委托，既有测试直连穿透。
        self._order_machine = OrderStateMachine(self.ctx, self._emit)

        # 已分发过周期边界的 1m stime 集合——同根 bar 二次触发时挡掉重复周期分发。
        # BarPoller 按 code 独立判定完成：慢股票在下一轮 poll 才完成同一 stime
        # （真机 14:30 被二次驱动，15m 二次白拉）。周期分发全局重拉全 stock_codes +
        # 周期策略二次求值 = 白拉；1m 策略不受影响（每轮 bar.stocks 只含当轮新完成
        # 股票，慢股票仍在其完成那轮被驱动求值）。stime 含日期，跨日无碰撞。
        # 属 _on_bar 编排状态（非行情数据本身），留引擎。
        self._dispatched_boundaries: set = set()

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
        # 15:05 收盘清扫已执行的日期标记（每日一次，确定性兜底未成交单）
        self._last_close_sweep_date: Optional[date] = None
        # _pending_orders / _last_backfill_time 已迁入 self._order_machine（0010 步骤 3）；
        # 引擎保留同名可读写 property 委托（recover 直接赋值 self._pending_orders = {...}、
        # 测试直连 engine._pending_orders 穿透到同一份字典）。
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
        # B5：SSE 事件总线（0010 步骤 1 抽到 core.engine.live.event_bus.EventBus）。
        # 持有订阅队列集合、跨线程投递、ping 心跳；引擎保留 _emit/stream_events 薄委托，
        # 并暴露 _stream_subscribers/_stream_ping_interval 兼容测试直连。
        self._event_bus = EventBus(ping_interval=stream_ping_interval, clock=now_shanghai)

    @property
    def _stream_subscribers(self) -> List["asyncio.Queue"]:
        """兼容测试直连（append/extend/in）：转发到 EventBus 的订阅列表。"""
        return self._event_bus.subscribers

    @property
    def _stream_ping_interval(self) -> float:
        return self._event_bus.ping_interval

    @_stream_ping_interval.setter
    def _stream_ping_interval(self, value: float) -> None:
        self._event_bus.ping_interval = value

    # ---- EngineContext 委托（0010 步骤 2a）----
    # 这些字段已迁入 self.ctx；保留同名 property 让既有调用点与测试的直接赋值
    # （engine.portfolios=[...]、engine._code_period_count={...}、engine._dispatcher=...）
    # 穿透到同一份 ctx 对象，纯搬移不改行为。
    @property
    def session_id(self) -> int:
        return self.ctx.session_id

    @session_id.setter
    def session_id(self, value: int) -> None:
        self.ctx.session_id = value

    @property
    def portfolios(self) -> List[Portfolio]:
        return self.ctx.portfolios

    @portfolios.setter
    def portfolios(self, value: List[Portfolio]) -> None:
        self.ctx.portfolios = value

    @property
    def _dispatcher(self) -> HttpBridgeDispatcher:
        return self.ctx.dispatcher

    @_dispatcher.setter
    def _dispatcher(self, value: HttpBridgeDispatcher) -> None:
        self.ctx.dispatcher = value

    @property
    def _db_session_factory(self) -> Callable[[], Session]:
        return self.ctx.db_session_factory

    @_db_session_factory.setter
    def _db_session_factory(self, value: Callable[[], Session]) -> None:
        self.ctx.db_session_factory = value

    @property
    def _code_period_count(self) -> Dict[tuple, int]:
        return self.ctx.code_period_count

    @_code_period_count.setter
    def _code_period_count(self, value: Dict[tuple, int]) -> None:
        self.ctx.code_period_count = value

    # ---- MarketDataService 委托（0010 步骤 2b）----
    # 缓存/公式注入配置归 MarketDataService；保留可读写 property 让既有测试直连
    # （engine.signal_cache={...}、engine._tq_formula.compute_injected=...、
    # engine._preheat_cache[...] = ...）穿透到同一份服务对象。
    @property
    def signal_cache(self) -> Dict:
        return self.market_data.signal_cache

    @signal_cache.setter
    def signal_cache(self, value: Dict) -> None:
        self.market_data.signal_cache = value

    @property
    def _preheat_cache(self) -> Dict[tuple, dict]:
        return self.market_data._preheat_cache

    @property
    def _tq_formula(self):
        return self.market_data._tq_formula

    @property
    def _formula_by_strategy(self) -> Dict[int, str]:
        return self.market_data._formula_by_strategy

    @property
    def _formula_count(self) -> int:
        return self.market_data._formula_count

    @property
    def _formula_count_by_name(self) -> Dict[str, int]:
        return self.market_data._formula_count_by_name

    @property
    def _period_count(self) -> Dict[str, int]:
        return self.market_data._period_count

    @property
    def _strategy_periods(self) -> set:
        return self.market_data._strategy_periods

    # ---- OrderStateMachine 委托（0010 步骤 3）----
    # 在途单字典/最近回填时点归 OrderStateMachine；保留可读写 property 让 recover 的
    # 直接赋值（self._pending_orders = {...}）与测试直连穿透到同一份对象。
    @property
    def _pending_orders(self) -> Dict[int, "LiveOrder"]:
        return self._order_machine.pending_orders

    @_pending_orders.setter
    def _pending_orders(self, value: Dict[int, "LiveOrder"]) -> None:
        self._order_machine.pending_orders = value

    @property
    def _last_backfill_time(self) -> Optional[datetime]:
        return self._order_machine.last_backfill_time

    @_last_backfill_time.setter
    def _last_backfill_time(self, value: Optional[datetime]) -> None:
        self._order_machine.last_backfill_time = value

    # ---------------- B5 SSE 事件流（委托 EventBus，0010 步骤 1）----------------
    def _emit(self, event_type: str, payload: dict) -> None:
        """向所有 SSE 订阅队列广播（委托 EventBus.emit，线程安全语义见 event_bus）。"""
        self._event_bus.emit(event_type, payload)

    def _emit_to_subscribers(self, ev: dict) -> None:
        """兼容旧调用/测试：直接向订阅队列投递（委托 EventBus）。"""
        self._event_bus._emit_to_subscribers(ev)

    def stream_events(self) -> AsyncIterator[dict]:
        """SSE 事件流（委托 EventBus.stream；公共签名不变，live.py 与测试仍调此方法）。

        直接返回内部 async generator（不用 `async for ... yield` 包裹），这样调用方
        aclose() 直接作用于 bus 的生成器，其 finally（退订队列）立即执行。
        """
        return self._event_bus.stream(lambda: self._running)

    # ---------------- 行情/信号缓存（委托 MarketDataService，0010 步骤 2b）----------------
    # 以下方法/静态方法保留为薄委托：实现已迁到 core.engine.live.market_data.MarketDataService，
    # 缓存（_preheat_cache/signal_cache）归服务所有。保留同名入口让既有调用点与测试直连
    # （engine._preheat()、engine._get_bars_with_increment(...)、LiveEngine._bars_to_formula_df(...)
    # 等）无需改动，纯搬移不改行为。
    def _preheat(self) -> None:
        self.market_data.preheat()

    def _make_cache_entry(self, bars: list, count: int) -> dict:
        return self.market_data._make_cache_entry(bars, count)

    def _get_bars_with_increment(self, code: str, period: str, count: int) -> list:
        return self.market_data.get_bars_with_increment(code, period, count)

    @staticmethod
    def _bar_stime(bar: dict) -> Optional[datetime]:
        return MarketDataService._bar_stime(bar)

    @staticmethod
    def _sort_and_cap(bars: list, count: int) -> list:
        return MarketDataService._sort_and_cap(bars, count)

    @staticmethod
    def _max_stime(bars: list) -> Optional[datetime]:
        return MarketDataService._max_stime(bars)

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        """起 asyncio 循环任务，绑定 BarPoller.on_bar 回调。"""
        if self._running:
            return
        self._running = True
        # 审计 #3：捕获运行中的事件循环，供 _emit 跨线程 call_soon_threadsafe 回投。
        self._loop_ref = asyncio.get_running_loop()
        self._event_bus.bind_loop(self._loop_ref)
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
        self._event_bus.clear_loop()
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
            # P2：超时检查移主循环——deals 循环被 60s 主循环饿死（单 worker 串行，
            # 主循环拉 ~73 只行情耗 50-57s/轮），180s 超时实际 440s 才跑到。移到主循环
            # 60s 节拍，最坏 60s 延迟。#1 修复后受理即丢弃已即时 rejected，此超时退为
            # 兜底（桥未丢但 order_ref 迟迟匹配不上的边缘场景）。同 _persist_breaker_count
            # 的 db session 模式；_expire_stale_orders 自身不 commit，由本块负责。
            db_tick = self._db_session_factory()
            try:
                self._expire_stale_orders(db_tick)
                db_tick.commit()
            except Exception:  # noqa: BLE001
                db_tick.rollback()
                logger.exception("expire stale orders error (session %s)", self.session_id)
            finally:
                db_tick.close()
            # E5/E6：14:30 日终一次 update_daily（日内亏损/熔断次日恢复推进）
            self._maybe_daily_close()
            # C6(B/C)：14:30 日终一次 1d 快照 bar + 1w/1mon 通达信注入驱动
            self._maybe_daily_bars()
            # 15:05 收盘清扫：交易日收盘后对仍未完结的单做确定性兜底（成交回填 +
            # 终态同步 + 剩余按 A 股收盘未成交=撤单标 canceled）。实时轮询的权威收口。
            self._maybe_close_sweep()
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
        # 非交易日（周末/节假日）不算日终：避免周末/假期 14:30 用陈旧 bar 重复估值、
        # 误触 daily_loss 或错误刷新 prev_close。桥日历离线时 fail-open（工作日照常）。
        if not self._calendar.is_trading_day(today):
            return
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
                now_paused = portfolio.risk_manager.daily_pause_active
                # B5：日内亏损熔断刚触发（daily_pause_active 变 True）→ 推送风控事件
                if now_paused and not was_paused:
                    # 触发幅度（可观测性）：日终市值/昨收/日内盈亏/亏损%/阈值。
                    dpm = portfolio.risk_manager
                    prev = dpm.prev_close_value
                    init_cap = portfolio.account.initial_capital
                    if prev is not None and init_cap > 0:
                        pnl = total - prev
                        loss_pct = abs(pnl) / init_cap * 100
                        detail = (
                            " total=%s prev_close=%s pnl=%s loss=%.2f%% threshold=%s"
                            % (total, prev, pnl, loss_pct, dpm.daily_loss_limit)
                        )
                    else:
                        detail = ""
                    self._emit("risk", {
                        "portfolio_id": portfolio.portfolio_id,
                        "rule": "daily_loss",
                        "triggered": True,
                        "total_value": str(total),
                        "prev_close_value": str(prev) if prev is not None else None,
                        "message": "日内亏损熔断触发，当日暂停新开仓",
                    })
                    logger.warning(
                        "circuit breaker: portfolio %s daily_loss 触发%s "
                        "on %s (当日暂停新开仓，次日自动恢复) (session %s)",
                        portfolio.portfolio_id, detail, today, self.session_id,
                    )
                elif was_paused and not now_paused:
                    logger.info(
                        "circuit breaker: portfolio %s daily_loss 次日自动恢复 "
                        "on %s (session %s)",
                        portfolio.portfolio_id, today, self.session_id,
                    )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "update_daily error (portfolio %s, date %s)",
                    portfolio.portfolio_id, today,
                )
        logger.info(
            "daily close done (session %s, %s): %d portfolio(s) valuated",
            self.session_id, today, len(self.portfolios),
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
        # 非交易日不驱动日线（同上：周末/假期不用陈旧快照触发 1d/1w/1mon 信号）。
        if not self._calendar.is_trading_day(today):
            return
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
        reinjected = self._startup_periods_missing(daily_time)
        if reinjected:
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
        logger.info(
            "daily bar driven (session %s, %s): %s snapshot over %d code(s), "
            "1w/1mon reinject=%s",
            self.session_id, daily_time, "1d", len(bars_by_code),
            reinjected,
        )

    # ---------------- 收盘清扫（15:05 确定性兜底）----------------
    def _maybe_close_sweep(self, now: Optional[datetime] = None) -> None:
        """交易日 15:05 后对当日仍未完结单做一次确定性兜底（幂等）。

        实时轮询（_poll_deals，5s）是「尽力而为」：桥抖动/被饿死/ORDER 实时表移除单
        都可能让单残留 submitted/partial。收盘后状态已全部收敛，做一次权威清扫：
          1) /orders + /deals 各查一次，补 order_ref 定位 + 按 order_ref/remark 回填成交；
          2) _sync_terminal_order_status 处理 53部撤/54全撤/57废单（含 partial 先 apply 再 cancel）；
          3) A 股规则：收盘仍未成交（含 48-52 在途/55 部成/实时表缺席）的单一律标 canceled
             ——收盘后不会再有成交，未成交部分被交易所/券商自动撤。partial 已回填的部分成交
             先 apply 落持仓再 cancel（与 53 部撤同处理，不丢成交）。
        与实时轮询的关系：清扫是「权威收口」，实时是「近实时反馈」；清扫幂等可重复执行。
        非交易日/桥离线：跳过（下轮或下个交易日重试，不误标）。
        """
        if now is None:
            now = now_shanghai()
        if (now.hour, now.minute) < _MARKET_CLOSE_SWEEP_TIME:
            return
        today = now.date()
        if not self._calendar.is_trading_day(today):
            return
        if self._last_close_sweep_date == today:
            return
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
                self._last_close_sweep_date = today
                logger.info(
                    "close sweep: no pending orders, nothing to do (session %s, %s)",
                    self.session_id, today,
                )
                return
            logger.info(
                "close sweep start (session %s, %s): %d pending order(s)",
                self.session_id, today, len(pending),
            )
            # 1) 一次 /orders + /deals：补 ref + 回填成交（复用实时轮询同套逻辑）。
            try:
                orders = self._dispatcher.query_orders()
            except BridgeUnavailableError:
                logger.warning("close sweep: bridge offline (/orders), skip")
                return
            claimed_refs = set(
                r[0] for r in db.query(LiveOrder.order_ref).filter(
                    LiveOrder.live_session_id == self.session_id,
                    LiveOrder.order_ref.isnot(None),
                ).all()
            )
            for lo in pending:
                if lo.order_ref is None:
                    self._try_match_order_ref(lo, claimed_refs, orders=orders)
                    if lo.order_ref is not None:
                        claimed_refs.add(lo.order_ref)
            db.commit()

            try:
                deals = self._dispatcher.query_deals()
            except BridgeUnavailableError:
                deals = None
            if deals is not None:
                for lo in pending:
                    if lo.status not in ("submitted", "partial"):
                        continue
                    if lo.order_ref is not None:
                        matched = [d for d in deals if d.get("order_ref") == lo.order_ref]
                        if matched:
                            self._backfill_order(db, lo, matched)
                            continue
                    if lo.bridge_order_id:
                        expected_remark = lo.bridge_order_id[:20]
                        matched = [d for d in deals if d.get("remark") == expected_remark]
                        if matched:
                            self._backfill_order(db, lo, matched)
                db.commit()

            # 2) 终态同步：53/54 → canceled（partial 先 apply）、57 → rejected。
            self._sync_terminal_order_status(db, pending, orders, deals)
            db.commit()

            # 3) A 股收盘兜底：仍在 submitted/partial 的单 = 收盘未成交 → canceled。
            #    实时表缺席（已撤单被移除）或状态仍在途（48-52/55/255）都落此：收盘后
            #    不会再成交，未成交即撤。已有部分成交（filled_quantity>0）先 apply 再 cancel。
            #
            #    资金安全：若 /orders 报 traded_volume > filled_quantity，说明 /deals 成交回报
            #    滞后（成交价/量/金额尚未回填齐），此时绝不能 cancel——会丢真实成交。延后本轮
            #    （不置 _last_close_sweep_date），下轮 60s tick 等 /deals 追上再清扫，与
            #    _sync_terminal_order_status 同一守卫。仅对 /orders 里**确实查到**的单可比
            #    traded_volume；实时表缺席的单无此字段，按 A 股规则直接 cancel（不会再有成交）。
            by_ref = {}
            for o in (orders or []):
                ref = o.get("order_ref")
                if ref is not None:
                    by_ref[ref] = o
            deferred = 0
            for lo in pending:
                if lo.status not in ("submitted", "partial"):
                    continue
                o = by_ref.get(lo.order_ref) if lo.order_ref is not None else None
                if o is not None:
                    traded_volume = int(o.get("traded_volume") or 0)
                    if traded_volume > 0 and int(lo.filled_quantity or 0) < traded_volume:
                        deferred += 1
                        logger.warning(
                            "close sweep: order %s %s %s deferring — /orders traded_volume=%s "
                            "ahead of filled=%s, waiting for /deals backfill",
                            lo.id, lo.trade_type, lo.stock_code,
                            traded_volume, int(lo.filled_quantity or 0),
                        )
                        continue
                if int(lo.filled_quantity or 0) > 0:
                    # 已有部分成交（partial，或 submitted 带成交的不一致态）：先 apply 落持仓
                    # 再 cancel——撤单后不会再有成交，这是最后的 apply 时机。
                    trade = (
                        db.query(LiveTrade)
                        .filter(LiveTrade.live_order_id == lo.id)
                        .first()
                    )
                    if trade is not None:
                        self._apply_filled_trade(
                            lo, trade.price, trade.quantity, trade.amount, trade.commission
                        )
                lo.status = "canceled"
                if not lo.error_message:
                    lo.error_message = "close sweep: unfilled remainder canceled after market close"
                self._pending_orders.pop(lo.id, None)
                logger.info(
                    "close sweep: order %s %s %s marked canceled (filled=%s/%s)",
                    lo.id, lo.trade_type, lo.stock_code,
                    int(lo.filled_quantity or 0), lo.quantity,
                )
                self._emit("order", {
                    "portfolio_id": lo.portfolio_strategy_id,
                    "order_id": lo.id,
                    "status": "canceled",
                    "stock_code": lo.stock_code,
                    "filled_quantity": int(lo.filled_quantity or 0),
                    "error_message": lo.error_message,
                })
            db.commit()
            self._sync_pending_orders(db)
            if deferred:
                # 有成交回报滞后的单：本轮不标记完成，下轮 60s tick 重试，等 /deals 回填齐。
                logger.warning(
                    "close sweep: %d order(s) deferred for /deals backfill, will retry "
                    "(session %s, %s)", deferred, self.session_id, today,
                )
                return
            self._last_close_sweep_date = today
            logger.info("close sweep done (session %s, %s)", self.session_id, today)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("close sweep error (session %s)", self.session_id)
        finally:
            db.close()

    # ---------------- bar 驱动 ----------------
    def _on_bar(self, bar: BarEvent) -> None:
        """BarPoller 回调（1m 节拍）：① 驱动 1m 策略；② 按 bar stime 边界分发长周期。

        边界判定只读 1m bar stime（periods_on_boundary），不引入本机时钟；
        5m/15m/30m/1h 策略在边界时点才被驱动（C6(A)），1m 节拍不再每 bar 算长周期。
        C4(#28)：df_cache/raw_cache 建在此（跨组合共享）——同 (code,period) 只 query_quote
        一次、同 (code,period,formula) 只 compute_injected 一次（TQ 计算最贵）。
        BarPoller 透传的本轮 bars（bar.bars_by_code）直接给 _handle_bar 注入复用——
        1m 判完成与算公式共用一次拉取，消除双拉（BarPoller 已拉 count=10，注入不再增量重拉）。
        """
        df_cache: Dict = {}
        raw_cache: Dict = {}
        bars_by_code = getattr(bar, "bars_by_code", None) or None
        for portfolio in self.portfolios:
            try:
                self._handle_bar(
                    portfolio, bar, bars_by_code=bars_by_code,
                    df_cache=df_cache, raw_cache=raw_cache,
                )
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

    # 非日内周期：bar_time 按约定是当日 00:00，只判交易日，不卡 09:30–15:00 时段。
    _DAILY_PERIODS = ("1d", "1w", "1mon")

    def _trading_allowed_for(self, bt: datetime, period: str) -> bool:
        """bar_time 是否允许下新单。日内周期判交易日+交易时段；日线及以上只判交易日。"""
        if period in self._DAILY_PERIODS:
            return self._calendar.is_trading_day(bt.date())
        return self._calendar.is_trading_allowed(bt)

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
        rm = portfolio.risk_manager
        was_broken = rm.circuit_breaker_active
        was_manual = rm.manual_recovery
        total_value = portfolio.total_value(bar)
        rm.update_peak(total_value, bar.bar_time.date())
        # max_drawdown 次日自动恢复（非手动恢复）此前在 risk_manager 内静默置 False，这里补日志
        if was_broken and not rm.circuit_breaker_active and not was_manual:
            logger.info(
                "circuit breaker: portfolio %s max_drawdown 次日自动恢复 "
                "(session %s, %s)",
                portfolio.portfolio_id, self.session_id, bar.bar_time.date(),
            )
        # H4：熔断计数持久化——update_peak 可能触发 max_drawdown（计数+1），计数变化才落库
        self._persist_breaker_count(portfolio, total_value)
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
                # 收盘后不下单守卫：bar 结束时间（bar_time）≥ 15:00（深交所收盘）则信号
                # 已推但跳过落单。根因：真机 2026-08-14 id40-41 的 bar_time=15:02:42（已过
                # 15:00），报不进交易所成待报单冻结持仓永不成交。用 bar_time（非墙钟：
                # 不依赖本机时钟/时区，测试与真实部署一致；主循环 60s 延迟不影响——延迟
                # 处理的是 15:00 前的合法 bar，其 bar_time<15:00 不被拦，正常下单）。
                # 不写 DB、不进 pending。
                bt = order.bar_time
                if bt is not None and (bt.hour, bt.minute) >= _MARKET_CLOSE_TIME:
                    logger.info(
                        "skip after-close order %s %s %s bar=%s (bar_time >= 15:00)",
                        order.trade_type.value, order.stock_code, order.signal_name,
                        bt,
                    )
                    continue
                # 交易日历总闸：非交易日不下新单。用 bar_time（非墙钟，见上方收盘守卫同理）。
                # 桥日历时 fail-open（工作日放行），只在权威日历明确判定"非交易"时才拦——
                # 周末/节假日绝不误下，真实交易日不误挡。风控 update_peak 与 signal 事件仍
                # 照常执行（只拦落单）。
                # 日内周期(1m/5m...)额外卡交易时段 09:30–15:00；1d/1w/1mon 的 bar_time 按
                # 约定是当日 00:00（由 14:30 _maybe_daily_bars 统一驱动），不套时段门，
                # 只判交易日，否则会把 14:30 合法驱动的日线单误杀。
                if bt is not None and not self._trading_allowed_for(bt, bar.period):
                    logger.info(
                        "skip non-trading order %s %s %s bar=%s period=%s (calendar/closed)",
                        order.trade_type.value, order.stock_code, order.signal_name,
                        bt, bar.period,
                    )
                    continue
                # BUY 首次建仓：确保 Position 存在（同回测 BacktestEngine；submitted 不 apply）
                pos = ctx.positions.get(order.stock_code)
                if pos is None and order.trade_type == TradeType.BUY:
                    pos = Position(order.stock_code)
                    ctx.positions[order.stock_code] = pos
                # F5/BUY 量上限：先定最终下单量（账户资金审批 / 桥 T+1 可用）再落
                # submitted——DB 下单量与实发一致，回填不误判 partial。None=不通过不下单。
                capped = self._engine.cap_quantity(order, portfolio.account, pos)
                if capped is None:
                    logger.info(
                        "skip order %s %s %s: cap_quantity None（资金/持仓上限拦截，不下单）",
                        order.trade_type.value, order.stock_code, order.signal_name,
                    )
                    continue
                order.quantity = capped
                # 跨重启去重门（D6）：同 (组合/策略/股票/bar_time/方向) 已有未完结单则跳过。
                # 根因：14:30 后页面停止/再启动 = Core 重启 = 新 LiveEngine = 实例内存丢失，
                # 1d/1w/1mon 会被重新驱动到同一 bar_time，且 order_id=live_order.id（自增 PK）
                # 每次重启都新号，桥侧 _placed 幂等无效 → 重复下单。
                # 按 (策略+股票+bar_time+方向) 去重：分钟策略不同 bar（10:00 vs 10:05）不被误杀、
                # 同组合多策略同 bar 同股各自开仓不被误拦，仅「同策略重驱到同 bar 同方向」才拦。
                # BUY/SELL 对称去重：重复 SELL 危害不亚于重复 BUY（已卖完再卖 → 超卖/桥拒单噪音）。
                # 分钟策略不同 bar 的合法平仓不受影响（bar_time 不同即不拦）。
                if order.bar_time is not None:
                    op = "buy" if order.trade_type == TradeType.BUY else "sell"
                    dup = db.query(LiveOrder).filter(
                        LiveOrder.live_session_id == self.session_id,
                        LiveOrder.portfolio_strategy_id == order.portfolio_id,
                        LiveOrder.strategy_id == order.strategy_id,
                        LiveOrder.stock_code == order.stock_code,
                        LiveOrder.bar_time == order.bar_time,
                        LiveOrder.trade_type == op,
                        LiveOrder.status.in_(["submitted", "partial", "filled"]),
                    ).first()
                    if dup is not None:
                        logger.info(
                            "skip dup %s %s %s bar=%s already placed (order %s)",
                            op.upper(), order.stock_code, order.signal_name,
                            order.bar_time, dup.id,
                        )
                        continue
                # 在途单门（F7）：同 (组合,策略,股票) 已有同向未确认单（submitted/partial）
                # → 压掉新同向单。根因：下单→/deals 成交回填之间存在在途窗口，期间虚拟
                # 持仓未更新，连续 bar 的 OPEN/CLOSE 信号看持仓是旧的 → 同股同向重复
                # 买单/卖单（真机 2026-08-13：159888/159929/159936 两根连续 bar 两次 OPEN）。
                # 按「同向」拦：在途 BUY 不拦 SELL、在途 SELL 不拦 BUY——风控止损/平仓
                # 卖出不被未确认的买入挡住。bar_time 不是放行理由（不同 bar 只是两次
                # 信号时点，上一单未确认就再下同向单仍是重复）。回填确认（filled）后由
                # portfolio.py 持仓守卫接管；rejected 释放门（被拒不会成交，必须允许重试）。
                # 终态（filled/canceled/rejected）都不在 submitted/partial 内，正确释放门；
                # 仅在途单占门。比重复买入更安全（prType=14 秒成秒回填，正常路径不受影响）。
                op = "buy" if order.trade_type == TradeType.BUY else "sell"
                inflight = db.query(LiveOrder).filter(
                    LiveOrder.live_session_id == self.session_id,
                    LiveOrder.portfolio_strategy_id == order.portfolio_id,
                    LiveOrder.strategy_id == order.strategy_id,
                    LiveOrder.stock_code == order.stock_code,
                    LiveOrder.trade_type == op,
                    LiveOrder.status.in_(["submitted", "partial"]),
                ).first()
                if inflight is not None:
                    logger.info(
                        "skip inflight %s %s %s bar=%s (order %s still %s)",
                        op.upper(), order.stock_code, order.signal_name,
                        order.bar_time, inflight.id, inflight.status,
                    )
                    continue
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
                logger.info(
                    "order accepted: id=%s %s %s %s qty=%s price=%s bar=%s (session %s)",
                    live_order.id, order.trade_type.value, order.stock_code,
                    order.signal_name, live_order.quantity, live_order.price,
                    order.bar_time, self.session_id,
                )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _persist_breaker_count(
        self, portfolio: Portfolio, total_value: Decimal = None
    ) -> None:
        """H4：把组合 max_drawdown 累计触发次数持久化到 LiveSessionPortfolio.circuit_breaker_count。

        每 bar update_peak 后比对：计数未变则不落库（避免每 bar 写）；变化（熔断触发 / 达 3 次
        转手动）→ 写 count，达 3 次 status 转 circuit_broken（design §8.3）。找不到 link
        （组合未关联本 session）→ 跳过。写库失败不阻断交易，记日志。

        total_value：触发时的组合总市值（来自 _handle_bar），仅用于把 total/peak/回撤幅度
        打进触发日志，便于事后核算（不参与落库逻辑）。None 时日志退化为不带幅度。
        """
        rm = portfolio.risk_manager
        count = rm.consecutive_drawdown_triggers
        old = self._breaker_count_written.get(portfolio.portfolio_id)
        if old == count:
            return
        # B5：计数递增（max_drawdown 熔断刚触发）→ 推送风控事件（首 bar old=None 不推）
        if old is not None and count > old:
            # 触发幅度（可观测性）：total/peak/回撤%/阈值。peak 在 update_peak 内是先抬峰再
            # 判回撤，触发那根 bar 的 peak 即触发热值；drawdown=(peak-total)/peak。
            if total_value is not None and rm.peak_value > 0:
                dd_pct = (rm.peak_value - total_value) / rm.peak_value * 100
                detail = (
                    " total=%s peak=%s drawdown=%.2f%% threshold=%s"
                    % (total_value, rm.peak_value, dd_pct, rm.max_drawdown)
                )
            else:
                detail = ""
            self._emit("risk", {
                "portfolio_id": portfolio.portfolio_id,
                "rule": "max_drawdown",
                "triggered": True,
                "count": count,
                "total_value": str(total_value) if total_value is not None else None,
                "peak_value": str(rm.peak_value),
                "drawdown_pct": (
                    float((rm.peak_value - total_value) / rm.peak_value)
                    if total_value is not None and rm.peak_value > 0 else None
                ),
                "message": "最大回撤熔断触发（累计 %d 次）" % count,
            })
            if count >= 3:
                logger.warning(
                    "circuit breaker: portfolio %s max_drawdown 触发%s "
                    "(累计 %d 次) → 转手动恢复，停新开仓等人工介入 (session %s)",
                    portfolio.portfolio_id, detail, count, self.session_id,
                )
            else:
                logger.warning(
                    "circuit breaker: portfolio %s max_drawdown 触发%s "
                    "(累计 %d 次，次日自动恢复) (session %s)",
                    portfolio.portfolio_id, detail, count, self.session_id,
                )
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

    def recover_breaker(self, portfolio_id: int) -> bool:
        """手动恢复某组合熔断：清零计数 + 解除手动恢复 + 落库 status=active。

        3 次转手动恢复（§8.3）的人工恢复入口。重置内存态 + DB LiveSessionPortfolio。
        peak_value 保持当前值（不重置历史峰值，回撤基准不变）。
        返回 True 若找到组合并恢复，False 若组合不属本 session。
        """
        port = next((p for p in self.portfolios if p.portfolio_id == portfolio_id), None)
        if port is None:
            return False
        rm = port.risk_manager
        rm.consecutive_drawdown_triggers = 0
        rm.circuit_breaker_active = False
        rm.manual_recovery = False
        rm.breaker_trigger_date = None
        # 同步已落库计数，否则下一 bar _persist_breaker_count 比对 old==count 跳过回写
        self._breaker_count_written[portfolio_id] = 0
        db = self._db_session_factory()
        try:
            link = db.query(LiveSessionPortfolio).filter_by(
                session_id=self.session_id,
                portfolio_strategy_id=portfolio_id,
            ).first()
            if link is not None:
                link.circuit_breaker_count = 0
                link.status = "active"
                db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("recover_breaker persist failed: portfolio %s", portfolio_id)
        finally:
            db.close()
        self._emit("risk", {
            "portfolio_id": portfolio_id, "rule": "max_drawdown",
            "triggered": False, "count": 0, "message": "熔断已手动恢复（计数清零）",
        })
        logger.info(
            "circuit breaker: portfolio %s 手动恢复（计数清零，解除停新开仓）(session %s)",
            portfolio_id, self.session_id,
        )
        return True

    def _dispatch_period_bar(self, period: str, boundary_time: datetime) -> None:
        """C6(A) 边界分发（薄委托 → MarketDataService.dispatch_period_bar）。"""
        self.market_data.dispatch_period_bar(period, boundary_time)

    # ---------------- F5：桥可用持仓（SELL 减仓上限）----------------
    def _refresh_available_map(self) -> None:
        """F5：拉桥 /positions 聚合 m_nCanUseVolume（薄委托 → MarketDataService）。"""
        self.market_data.refresh_available_map()

    # ---------------- 公式信号注入（0010 + C4 #28 三维去重）----------------
    def _fill_signal_cache(
        self,
        portfolio: Portfolio,
        bar: BarEvent,
        bars_by_code: Optional[Dict[str, list]] = None,
        df_cache: Optional[Dict] = None,
        raw_cache: Optional[Dict] = None,
    ) -> None:
        """实盘逐 bar 算公式信号填 signal_cache（薄委托 → MarketDataService）。"""
        self.market_data.fill_signal_cache(
            portfolio, bar, bars_by_code=bars_by_code,
            df_cache=df_cache, raw_cache=raw_cache,
        )

    def _fetch_cached_bars(
        self,
        df_cache: Dict,
        bars_by_code: Optional[Dict[str, list]],
        code: str,
        period: str,
        count: int,
    ) -> list:
        """拉取去重（单 bar 生命周期，薄委托 → MarketDataService）。"""
        return self.market_data._fetch_cached_bars(
            df_cache, bars_by_code, code, period, count
        )

    def _reuse_provided_with_cache(self, code: str, period: str, provided: list, count: int) -> list:
        """BarPoller 透传 bars 并入预热缓存复用（薄委托 → MarketDataService）。"""
        return self.market_data._reuse_provided_with_cache(code, period, provided, count)

    @staticmethod
    def _bars_to_formula_df(bars: list, code: str) -> Optional[dict]:
        """桥 bar dict 列表 → OHLCV DataFrame dict（薄委托 → MarketDataService）。"""
        return MarketDataService._bars_to_formula_df(bars, code)

    @staticmethod
    def _extract_latest_signal(raw: Optional[dict], code: str) -> List[dict]:
        """取公式返回最后一条 bar 的信号（薄委托 → MarketDataService）。"""
        return MarketDataService._extract_latest_signal(raw, code)

    def _inject_startup_periods(self, daily_time: datetime) -> None:
        """C6(C)：1w/1mon 通达信启动注入（薄委托 → MarketDataService）。"""
        self.market_data.inject_startup_periods(daily_time)

    def _startup_periods_missing(self, daily_time: datetime) -> bool:
        """1w/1mon 信号是否全部已预填（薄委托 → MarketDataService）。"""
        return self.market_data.startup_periods_missing(daily_time)

    # ---------------- 订单状态机 + 成交回报回填（委托 OrderStateMachine，0010 步骤 3）----------------
    # 下列方法为薄委托：实现已迁入 core.engine.live.order_machine.OrderStateMachine。
    # 同名方法/静态方法保留于此，既有调用点与测试直连（engine._poll_deals()、
    # engine._backfill_order(...)、engine._match_by_remark(...) 等）穿透到协作者。
    def _persist_order_submitted(self, db: Session, order: OrderEvent) -> LiveOrder:
        return self._order_machine.persist_order_submitted(db, order)

    def _try_match_order_ref(self, live_order: LiveOrder, claimed_refs=None,
                             orders=None) -> None:
        self._order_machine.try_match_order_ref(
            live_order, claimed_refs=claimed_refs, orders=orders)

    @staticmethod
    def _match_by_remark(orders, expected_remark, claimed):
        return OrderStateMachine.match_by_remark(orders, expected_remark, claimed)

    @staticmethod
    def _match_legacy_fuzzy(live_order, orders, claimed):
        return OrderStateMachine.match_legacy_fuzzy(live_order, orders, claimed)

    def _poll_deals(self) -> None:
        self._order_machine.poll_deals()

    def _sync_terminal_order_status(self, db: Session, pending: list,
                                    orders: Optional[list],
                                    deals: Optional[list]) -> None:
        self._order_machine.sync_terminal_order_status(db, pending, orders, deals)

    def _expire_stale_orders(self, db: Session, pending: Optional[list] = None) -> None:
        self._order_machine.expire_stale_orders(db, pending)

    def _sync_pending_orders(self, db: Session) -> None:
        self._order_machine.sync_pending_orders(db)

    def _backfill_order(self, db: Session, live_order: LiveOrder, matched_deals: list) -> None:
        self._order_machine.backfill_order(db, live_order, matched_deals)

    @staticmethod
    def _parse_trade_time(deal: dict) -> datetime:
        return OrderStateMachine.parse_trade_time(deal)

    def _apply_filled_trade(self, live_order: LiveOrder, price: Decimal,
                            qty: int, amount: Decimal, commission: Decimal) -> None:
        self._order_machine.apply_filled_trade(
            live_order, price, qty, amount, commission)

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
                logger.warning(
                    "circuit breaker: portfolio %s 重启读回累计 %d 次 → 转手动恢复，"
                    "停新开仓等人工介入 (session %s)",
                    link.portfolio_strategy_id, link.circuit_breaker_count, self.session_id,
                )
            else:
                logger.info(
                    "circuit breaker: portfolio %s 重启读回累计 %d 次（未转手动，正常运行）"
                    " (session %s)",
                    link.portfolio_strategy_id, link.circuit_breaker_count, self.session_id,
                )
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
