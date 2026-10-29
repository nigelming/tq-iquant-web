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
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import AsyncIterator, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from .portfolio import Portfolio
from .position import Position
from .execution_engine import ExecutionEngine, LiveT1Checker
from .event import BarEvent, OrderEvent, TradeEvent
try:
    from .http_bridge_dispatcher import (
        HttpBridgeDispatcher,
        BridgeUnavailableError,
        BridgeOrderRejected,
    )
except Exception:
    class BridgeUnavailableError(RuntimeError):
        pass

    class BridgeOrderRejected(RuntimeError):
        pass

    HttpBridgeDispatcher = object  # type: ignore
from .bar_poller import BarPoller
# TradingCalendar 为 live 专属（仅实盘引擎及其协作者使用，回测不依赖），
# 0010 后续迁入 live 子包；旧路径 core.engine.trading_calendar 保留 re-export shim。
from .live.calendar import TradingCalendar
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
from .live.breaker import BreakerService
from .live.daily_closer import DailyCloser
from .market_rules import session_allows_bar, tick_size
from .decision import DecisionRecorder
from .regime import CausalRegimeTracker
from core.models import LiveOrder, LiveTrade, LiveDecisionEvent
from core.tq.formula import TQFormula
from tq_iquant_shared.constants import SignalType, TradeType

logger = logging.getLogger(__name__)

# 深交所收盘时点（上海 15:00）。收盘后禁止新下单——报不进交易所，会成待报单
# 冻结持仓永不成交（真机 2026-08-14 id40-41，bar_time=15:02:42）。
# 用 bar.bar_time 判定（非墙钟：不依赖本机时钟/时区）。仅拦新落单，信号仍推。
_MARKET_CLOSE_TIME = (15, 0)
# _DAILY_CLOSE_TIME(14:30) / _MARKET_CLOSE_SWEEP_TIME(15:05) 与日终/收盘编排一并
# 迁入 core.engine.live.daily_closer.DailyCloser（0010 步骤 5）；收盘新落单拦截仍留
# 本模块（_MARKET_CLOSE_TIME，_handle_bar 内按 bar_time 判定）。

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
        regime_provider: Optional[Callable] = None,
        benchmark_codes: Optional[Dict[int, str]] = None,
        execution_mode: str = "auto",
        signal_monitor=None,
        signal_scheduler=None,
        formula_id_by_strategy: Optional[Dict[int, int]] = None,
        enable_signal_monitor: bool = False,
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
        # alert/auto mode
        self.execution_mode = execution_mode
        self._formula_id_by_strategy = formula_id_by_strategy or {}
        self._tq_alert = None
        self._signal_monitor = signal_monitor
        if enable_signal_monitor and signal_monitor is None:
            from ..tq.alert import TQAlert
            from .live.signal_monitor import SignalMonitor
            self._tq_alert = TQAlert()
            self._signal_monitor = SignalMonitor(
                session_id, db_session_factory, self._tq_alert, self._emit,
                execution_mode,
            )
        # 决策闸门采集（调参可观测性）：引擎级单例，注入执行引擎与各组合（set_recorder
        # 顺带透传到 risk_manager）。_handle_bar 每组合末尾 drain 落 LiveDecisionEvent + SSE。
        self._recorder = DecisionRecorder()
        for p in portfolios:
            p.set_recorder(self._recorder)
        self._poll_interval = poll_interval
        # B5 市场状态闸（默认关）：regime_provider(code, end_date) → {date: Decimal}
        # 基准收盘全量历史；每组合每日首个 bar 重建因果跟踪器并注入
        # portfolio.current_regime。provider 为 None（默认）时完全不启用，行为不变。
        self._regime_provider = regime_provider
        self._benchmark_codes: Dict[int, str] = benchmark_codes or {}
        self._regime_updated: Dict[int, date] = {}
        # R3：每组合每日首根 bar 已调 enter_day（跨日补算上一日 daily_loss，
        # 与回测对齐——暂停/恢复在日首生效），防混合周期多 bar/日重复调用。
        self._enter_day_updated: Dict[int, date] = {}
        # trading_session（full/am/pm）时段闸：每组合每日首次跳过记 1 条决策
        # 事件防刷屏（内存 set 按 (portfolio_id, date)，Core 重启重记无碍）。
        self._session_skip_recorded: set = set()
        # B-1 未来函数闸门（对齐回测）：日线级信号当日不下单，订单暂存此内存队列，
        # 次一交易日首 bar（bar_time.date() 严格小于当前 bar 日期）放行重走提交链。
        # 已知限制：队列仅内存，Core 重启即丢（信号边沿去重不会重发，无感知），
        # 持久化归 P2；stop() 时丢弃属预期。
        self._signal_bar_deferred: List[OrderEvent] = []
        # 批次1 3a 撤单追价重下：次数记账（引擎内存，重启清零可接受——
        # requote_max_attempts 次数上限兜底，重启后最坏多追价一轮）。
        # key=(strategy_id, stock_code, signal_name, bar_date)，value=已用重试次数。
        self._requote_attempts: Dict[tuple, int] = {}
        # G5：/deals 成交回报轮询独立节拍（默认 5s，比主循环 60s 更短）——成交秒级回报，
        # 持仓/资金反馈要近实时，不跟 bar 拉取同频。
        self._deals_poll_interval = deals_poll_interval
        # 实盘执行引擎：复用回测 ExecutionEngine，注入桥 dispatcher + 实盘 T+1 检查
        # F5：t1_checker 持每 bar 刷新的桥可用表（SELL 减仓上限用 m_nCanUseVolume）
        self._t1_checker = LiveT1Checker()
        self._engine = ExecutionEngine(dispatcher, self._t1_checker, recorder=self._recorder)
        # 执行契约文件交接层（file_handoff_enabled 门控，通道无关）：signal/decision/
        # risk 事件同步落 {work_dir}/{session_id}/ 下 signals/risk.jsonl，供外部
        # 诊断/对账。orders.jsonl 由 dispatcher 专职写（避免双写）；写失败不影响主流程。
        self._file_handoff = None
        if getattr(dispatcher, "file_handoff_enabled", False):
            try:
                from .live.file_handoff import FileHandoff
                self._file_handoff = FileHandoff(
                    getattr(dispatcher, "_work_dir", "data/ths"), session_id)
            except Exception:  # noqa: BLE001
                logger.warning("file handoff init failed", exc_info=True)

        # 行情/信号协作者（0010 步骤 2b）：预热/增量 bar 缓存、公式信号注入、周期边界
        # 分发、1w/1mon 通达信注入归 MarketDataService。on_bar 回调注入 self._handle_bar
        # （服务不反向 import LiveEngine，周期/日终 bar 经此回调交回引擎下单/风控）；
        # F5 可用持仓写回经 set_available_map 窄回调。引擎保留同名薄委托方法/property，
        # 既有调用点与测试直连（engine.signal_cache={...}、engine._preheat() 等）穿透。
        self.market_data = MarketDataService(
            self.ctx,
            bar_poller,
            on_bar=(
                self._handle_alert_bar
                if execution_mode == "alert" else self._handle_bar
            ),
            set_available_map=self._t1_checker.set_available_map,
            tq_formula=tq_formula,
            formula_by_strategy=formula_by_strategy,
            formula_count=formula_count,
            formula_count_by_name=formula_count_by_name,
        )
        self._signal_scheduler = signal_scheduler
        if (enable_signal_monitor and execution_mode == "alert"
                and signal_scheduler is None):
            from .live.signal_scheduler import SignalScheduler
            self._signal_scheduler = SignalScheduler(
                self.ctx, self.market_data, lambda: self._calendar,
                self.market_data._formula_by_strategy,
                self.market_data._formula_count_by_name,
                self._formula_id_by_strategy,
                self._signal_monitor, self._tq_alert,
            )
        # 委托状态机/成交回填协作者（0010 步骤 3）：order_ref 匹配、/deals 轮询回填、
        # 终态同步、陈旧单失效、filled apply 归 OrderStateMachine。持有 _pending_orders/
        # _last_backfill_time。事件发射经 self._emit 回调注入（不反向 import LiveEngine）。
        # 引擎保留全部同名方法/属性为薄委托，既有测试直连穿透。
        self._order_machine = OrderStateMachine(self.ctx, self._emit)
        # 熔断编排协作者（0010 步骤 4）：每 bar update_peak/max_drawdown、日终 daily_loss、
        # 计数持久化、3 次转手动、手动恢复、重启读回归 BreakerService。持有 _breaker_count_written。
        # 事件发射经 self._emit 回调注入（不反向 import LiveEngine）。引擎保留同名薄委托。
        self._breaker = BreakerService(self.ctx, self._emit)
        # 日终/收盘时点编排协作者（0010 步骤 5）：14:30 日终估值（转 breaker）、
        # 14:30 日线快照+1w/1mon 注入（转 market_data，经 on_bar 回调驱动）、15:05
        # 收盘清扫（转 orders 回填/终态同步/兜底 cancel）。持有三个幂等日期标记。
        # on_bar/emit/set_bridge_offline 经回调注入（不反向 import LiveEngine）；
        # 引擎保留三个 _maybe_* 薄方法 + 同名可读写 property 兼容测试直连。
        self._closer = DailyCloser(
            self.ctx,
            self.market_data,
            self._breaker,
            self._order_machine,
            on_bar=self._handle_bar,
            # 日历以 provider 注入：测试在构造后替换 engine._calendar，需读到最新值。
            calendar_provider=lambda: self._calendar,
            emit=self._emit,
            set_bridge_offline=lambda: setattr(self, "_bridge_online", False),
        )

        # 已分发过周期边界的 1m stime 集合——同根 bar 二次触发时挡掉重复周期分发。
        # BarPoller 按 code 独立判定完成：慢股票在下一轮 poll 才完成同一 stime
        # （真机 14:30 被二次驱动，15m 二次白拉）。周期分发全局重拉全 stock_codes +
        # 周期策略二次求值 = 白拉；1m 策略不受影响（每轮 bar.stocks 只含当轮新完成
        # 股票，慢股票仍在其完成那轮被驱动求值）。stime 含日期，跨日无碰撞。
        # 属 _on_bar 编排状态（非行情数据本身），留引擎。
        self._dispatched_boundaries: set = set()

        self._running = False
        # I-5：stop 请求标记（stop() 首行置 True、start() 重置 False）。提交链入口
        # 据此拒新单——_running 兼作「循环是否在跑」，直调 _handle_bar 的同步路径
        # （测试/ recover 前置）需要放行，故用独立标记表达「已请求停止」语义。
        self._stop_requested = False
        self._task: Optional[asyncio.Task] = None
        # G5：/deals 回填轮询的独立任务（与主循环并行，见 _deals_loop）
        self._deals_task: Optional[asyncio.Task] = None
        # 审计 #3：_loop 的同步阻塞 I/O（heartbeat/poll/_maybe_daily_*，全用同步
        # httpx.Client）整轮丢到此单 worker 线程执行——事件循环不再被阻塞 I/O 冻结。
        # max_workers=1 让主循环 tick 严格串行：signal_cache / 行情缓存等主池私有状态
        # 无并发竞争（沿用既有「同事件循环协作式」的串行语义，只是换到线程）。
        # 单 worker 还保证 cancel 后已排队的 tick 不与下一轮重叠（asyncio.Lock 在 cancel
        # 时会释放，无法保证；单 worker 天然排队）。
        self._executor: ThreadPoolExecutor = ThreadPoolExecutor(max_workers=1)
        # I-8（audit）：_deals_loop 的 tick 投独立单 worker 池——与主循环各自的
        # 1-worker 池互不饿死（真机：主循环拉 ~73 只行情 50-57s/轮时，5s 节拍的
        # deals 回填不再排队等主 tick）。共享资源由 TQ 锁 get_tdx_lock 串行化；
        # I-8/C2（终审）：两池**真并发**后，跨池记账（pending_orders 增删遍历、
        # backfill/apply、持仓现金更新）不再靠协作式约定，由引擎级 _book_lock 串行化
        # （锁序恒为 book_lock → get_tdx_lock，无反向获取，无死锁）。
        self._deals_executor: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="live-deals"
        )
        # C2（终审）：引擎级记账锁。主池（bar/approve/submit/超时收割/收盘清扫）与
        # deals 池（5s 回填）独立后，三处真并发冲突点由调用点持锁串行：
        #   ① _tick_main 超时收割段（expire 兜底 + cancel_unfilled 收割 apply）
        #   ② 收盘清扫 apply 段（_maybe_close_sweep → DailyCloser）
        #   ③ _tick_deals 回填段（poll_deals → backfill → apply / pending 增删）
        #   ④ _handle_bar 遍历 _pending_orders.values()（vs 跨池 pop/整字典替换）
        # RLock：锁内允许调 SDK（get_tdx_lock 不反向取本锁），嵌套持锁安全。
        self._book_lock = threading.RLock()
        # _emit 跨线程投递用：start() 时捕获运行中的事件循环；worker 线程内 _emit 经
        # call_soon_threadsafe 回到事件线程 put_nowait（asyncio.Queue 非线程安全）。
        # None = 尚未 start（直接同步调 _emit 的旧测试路径 → 直接 put_nowait）。
        self._loop_ref: Optional[asyncio.AbstractEventLoop] = None
        self._bridge_online = True
        # 三个幂等日期标记已迁入 self._closer（DailyCloser，0010 步骤 5）；下列赋值经
        # 同名 property 委托穿透到 closer 字段（初始化为 None，与 closer 默认一致）。
        self._last_daily_date = None
        self._last_daily_bar_date = None
        self._last_close_sweep_date = None
        # _pending_orders / _last_backfill_time 已迁入 self._order_machine（0010 步骤 3）；
        # 引擎保留同名可读写 property 委托（recover 直接赋值 self._pending_orders = {...}、
        # 测试直连 engine._pending_orders 穿透到同一份字典）。
        # F5：最近处理 bar 的强引用（多组合共享同一 bar 对象 → 每 bar 只刷一次桥可用
        # 持仓；强引用防对象 id 复用导致的漏刷）。None=尚未处理任何 bar。
        self._available_bar: Optional["BarEvent"] = None
        # D4/H4：每组合已持久化的熔断计数（LiveSessionPortfolio.circuit_breaker_count）
        # 已迁入 self._breaker（BreakerService，0010 步骤 4）；引擎保留同名可读写
        # property 委托（recover 直接赋值 self._breaker_count_written[pid]=n、测试直连
        # engine._breaker_count_written 穿透到同一份字典）。
        # D3：recover 对账结果——虚拟持仓 vs 桥 /positions 按 code 比对的差异列表
        # （{code, virtual, real, diff}）。只记录/告警，不自动修正账面（首期安全）。
        self._reconcile_mismatches: List[dict] = []
        # I-7：收盘复查对账的幂等日期标记（每交易日一次，见 _maybe_reconcile_after_close）
        self._last_reconcile_date: Optional[date] = None
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
        # 构造后替换组合列表（测试直连 / recover 重建）也要注入决策采集器
        rec = getattr(self, "_recorder", None)
        if rec is not None:
            for p in value:
                p.set_recorder(rec)

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
    # （engine.signal_cache={...}、engine._tq_formula.compute_injected_batch=...、
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

    # _breaker_count_written 归 BreakerService（0010 步骤 4）；返回活字典引用，
    # 让 recover 的 self._breaker_count_written[k]=n 与测试的 engine._breaker_count_written[k]=n
    # 穿透到同一份对象。
    @property
    def _breaker_count_written(self) -> Dict[int, int]:
        return self._breaker.counts_written

    @_breaker_count_written.setter
    def _breaker_count_written(self, value: Dict[int, int]) -> None:
        self._breaker.counts_written = value

    # ---- DailyCloser 委托（0010 步骤 5）----
    # 三个幂等日期标记归 DailyCloser；保留可读写 property 让测试直连
    # （engine._last_daily_date = today、engine._last_close_sweep_date 读判等）穿透。
    @property
    def _last_daily_date(self):
        return self._closer.last_daily_date

    @_last_daily_date.setter
    def _last_daily_date(self, value) -> None:
        self._closer.last_daily_date = value

    @property
    def _last_daily_bar_date(self):
        return self._closer.last_daily_bar_date

    @_last_daily_bar_date.setter
    def _last_daily_bar_date(self, value) -> None:
        self._closer.last_daily_bar_date = value

    @property
    def _last_close_sweep_date(self):
        return self._closer.last_close_sweep_date

    @_last_close_sweep_date.setter
    def _last_close_sweep_date(self, value) -> None:
        self._closer.last_close_sweep_date = value

    # ---------------- B5 SSE 事件流（委托 EventBus，0010 步骤 1）----------------
    def _emit(self, event_type: str, payload: dict) -> None:
        """向所有 SSE 订阅队列广播（委托 EventBus.emit，线程安全语义见 event_bus）。"""
        self._emit_file_handoff(event_type, payload)
        self._event_bus.emit(event_type, payload)

    def _emit_file_handoff(self, event_type: str, payload: dict) -> None:
        """挂了执行契约文件交接层的会话：signal/decision/risk 事件同步写交接文件。

        失败只 warning 不影响主流程（SSE 投递与落库是权威通道，交接文件是
        外部诊断/对账副本）。order 事件不落（orders.jsonl 由 dispatcher 专职写）。
        """
        handoff = self._file_handoff
        if handoff is None or event_type not in ("signal", "decision", "risk"):
            return
        try:
            entry = {"type": event_type, **payload}
            if event_type == "signal":
                handoff.write_signal(entry)
            else:
                handoff.write_risk(entry)
        except Exception:  # noqa: BLE001
            logger.warning("file handoff write failed (%s)", event_type, exc_info=True)

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
        self._stop_requested = False  # 重启支持：清掉上次 stop 的拒单闸
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
        # I-8：deals 独立池同理随 start 重建（stop 已 shutdown 旧池）
        if self._deals_executor is not None:
            try:
                self._deals_executor.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass
        self._deals_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="live-deals"
        )
        # I-6（audit）：启动一次性同步重活（通达信 1w/1mon 注入 + 全量预热拉取）
        # 经 executor 执行——事件循环不被 TDX 计算/桥拉取阻塞（SSE/HTTP 保持响应）。
        # await 等其完成再起循环，时序语义与原同步内联一致。
        await self._loop_ref.run_in_executor(self._executor, self._sync_startup)
        self._bar_poller.on_bar = self._on_bar
        self._task = asyncio.create_task(self._loop())
        # G5：独立 /deals 回填轮询（5s 节拍，不随主循环 60s 拉 bar 一起）
        if self.execution_mode != "alert":
            self._deals_task = asyncio.create_task(self._deals_loop())

    def _sync_startup(self) -> None:
        """start() 的一次性同步重活（I-6：在主 executor worker 线程执行）。

        C6(C)：启动时通达信注入 1w/1mon 策略信号（桥端 xtdata 拉不到，仅此通路），
        一次同步 TDX 计算（get_tdx_lock 串行）；随后预热 1m/5m/15m/30m/1h 历史 bar
        到 _preheat_cache（运行期增量拼接，省每 bar/每边界全量重拉）。必须在 _loop
        起来前完成（否则首个 bar 触发时缓存未就绪）；单 (code,period) 失败不阻断，
        运行期自愈。
        """
        if self.execution_mode != "alert":
            self._inject_startup_periods(
                datetime.combine(now_shanghai().date(), datetime.min.time())
            )
        self._preheat()

    def _dispatcher_close_safe(self) -> None:
        """TQ 1.3.1：释放 dispatcher 资源（best-effort，可安全重复调用）。

        stop() 经主 executor 执行本方法（I-5）：close 排队于在途 tick 之后——
        在途 tick 持 get_tdx_lock 跑完后 close 自然获得锁，事件循环不被 TDX
        退订对账阻塞。TdxTqDispatcher.close 做行情退订对账；其余通道 close 为
        轻量/无操作；iQuant 桥无 close 则跳过。失败仅告警，不阻断停止流程。
        """
        close = getattr(self._dispatcher, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001
                logger.warning("dispatcher close failed on stop", exc_info=True)

    def _cancel_inflight_orders_on_stop(self) -> None:
        """I-3（终审）：stop 时对在途真实单 best-effort 撤单（主 executor worker 执行）。

        在途集合以 DB 为准（submitted/partial，与 poll_deals/sync_pending_orders 同一
        口径；_pending_orders 缓存的 ORM 实例可能已随原 session 关闭而 detached）：
        有 order_ref → 尽力 ``dispatcher.cancel_order(ref, code)``（复用 A-1 撤单；
        异常吞并计数）；通道不支持撤单（如 iQuant 桥）或单无 order_ref → 计入风险汇总。
        停止后不再有轮询收敛这些单，撤单失败/无 ref 的部分汇成**一条**
        risk 事件（中文，工作台事件日志可见），提示人工核对柜台委托。
        """
        factory = self._db_session_factory
        if factory is None:
            return
        db = factory()
        if db is None:
            # alert 模式（无 DB、不下单）：无在途单可撤
            return
        try:
            inflight = (
                db.query(LiveOrder)
                .filter(
                    LiveOrder.live_session_id == self.session_id,
                    LiveOrder.status.in_(["submitted", "partial"]),
                )
                .all()
            )
            if not inflight:
                return
            logger.info(
                "stop: %d in-flight order(s) at stop, best-effort cancel (session %s)",
                len(inflight), self.session_id,
            )
            cancel = getattr(self._dispatcher, "cancel_order", None)
            failed: List[str] = []
            no_ref: List[str] = []
            for lo in inflight:
                label = "%s %s" % (lo.stock_code, (lo.trade_type or "").upper())
                ref = lo.order_ref
                if not ref:
                    no_ref.append(label)
                    continue
                if not callable(cancel):
                    failed.append("%s（%s，通道不支持撤单）" % (ref, label))
                    continue
                try:
                    if cancel(ref, lo.stock_code):
                        logger.info(
                            "stop: cancel requested for in-flight order %s (ref=%s)",
                            lo.id, ref,
                        )
                    else:
                        failed.append("%s（%s，撤单返回失败：%s）" % (
                            ref, label,
                            getattr(self._dispatcher, "last_error", "") or "无原因"))
                except Exception as e:  # noqa: BLE001 — best-effort：单笔失败不阻断停止
                    failed.append("%s（%s，撤单异常：%s）" % (ref, label, e))
                    logger.warning(
                        "stop: cancel_order error for order %s ref %s: %s", lo.id, ref, e,
                    )
            if not failed and not no_ref:
                return
            parts = []
            if failed:
                parts.append("撤单失败 %d 笔：%s" % (len(failed), "；".join(failed)))
            if no_ref:
                parts.append(
                    "无 order_ref 在途单 %d 笔（%s），停止后无法追踪终态"
                    % (len(no_ref), "、".join(no_ref))
                )
            message = (
                "会话停止时存在未完结在途单——%s，请人工核对柜台委托与成交" % "；".join(parts)
            )
            self._emit("risk", {"rule": "stop_inflight_orders", "message": message})
            logger.warning("stop: %s (session %s)", message, self.session_id)
        finally:
            db.close()

    async def stop(self) -> None:
        """停循环任务。"""
        self._running = False
        # I-5：先置拒单闸——cancel 期间在途 tick 若再走 _handle_bar 不发新单
        self._stop_requested = True
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
        # I-3（终审）：置闸后、close 前对在途真实单 best-effort 撤单。此刻两循环任务
        # 已 cancel，在途 tick 排在主单 worker 池里先跑完，本步经同一池串行执行——
        # 读到的 _pending_orders 不再被并发修改。撤单走 worker 线程（SDK/HTTP 同步调），
        # risk 汇总事件经 EventBus 跨线程回投（loop 引用尚未清空）。
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._executor, self._cancel_inflight_orders_on_stop
            )
        except RuntimeError:
            self._cancel_inflight_orders_on_stop()
        # 审计 #3：停止后清空 loop 引用——worker 线程已不再产生 _emit；
        # 之后若再有同步直调 _emit（测试 / 外部），走「无 loop」直接 put_nowait 路径。
        self._loop_ref = None
        self._event_bus.clear_loop()
        # B-1：停会话丢弃 deferred 日线信号订单（预期行为——未提交的信号不跨会话补发）。
        # I-3（终审）：清空条数记 info 日志，留运行痕迹。
        if self._signal_bar_deferred:
            logger.info(
                "stop: drop %d deferred daily-signal order(s) (session %s)",
                len(self._signal_bar_deferred), self.session_id,
            )
        self._signal_bar_deferred.clear()
        # I-5：dispatcher.close 经同一主 executor 执行——在途 tick 持 get_tdx_lock
        # 跑完后 close 自然获得锁（同池 max_workers=1 串行排队），事件循环不被
        # TDX 退订对账阻塞（原先在事件循环线程同步 close，秒级冻结）。
        # RuntimeError = executor 已 shutdown（重复 stop）→ 同步兜底（close 幂等）。
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._executor, self._dispatcher_close_safe
            )
        except RuntimeError:
            self._dispatcher_close_safe()
        # I-8：两池都 shutdown（wait=False：close 已在上方 await 完成，在途 tick
        # 是 loopback HTTP/本地计算，瞬间完成；任务已 cancel 不会再提交新 tick）。
        # 释放非 daemon worker 线程，防进程退出挂起。
        if self._executor is not None:
            self._executor.shutdown(wait=False)
        if self._deals_executor is not None:
            self._deals_executor.shutdown(wait=False)

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
            if self.execution_mode == "alert":
                self._tick_alert()
                return
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
                self._sweep_timeout_orders(db_tick)
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
            # I-7（audit）：收盘流程完成后每交易日一次对账复查（虚拟 vs 桥实际）——
            # 此时在途单已终态收敛、成交已回填，差异最可信。
            self._maybe_reconcile_after_close()
        except BridgeUnavailableError as e:
            self._bridge_online = False
            logger.warning("bridge unavailable: %s, skip this round", e)
        except Exception:  # noqa: BLE001
            logger.exception("live loop unexpected error")

    def _sweep_timeout_orders(self, db: Session) -> None:
        """主循环超时收割段（P2 + A-2）：expire 兜底收割 + 超时撤单收割，同块提交。

        C2（终审）：expire 的 remark 兜底回填与 cancel 成功后的即时收割都会
        backfill/apply 持仓现金——与 deals 池 5s 回填真并发，整段（含 commit）
        持 _book_lock 串行化。commit 必须在锁内：后进锁的 deals 回填据此看到
        已提交的终态（filled/partial），不会对同一单重复 backfill/apply。

        批次1 3a：撤单收敛 canceled 后，对开启 requote_enabled 的策略做有限
        追价重下（重下走 _submit_one_order 同款提交链，仍持 _book_lock——
        pending_orders 增删须与 deals 池串行；锁序恒 book_lock → 通道锁，无反向）。
        """
        with self._book_lock:
            self._expire_stale_orders(db)
            # A-2（I-4/C-1）：在途单超时未成交 → 真实撤单（与 expire 同块提交；
            # 配置 cancel_unfilled_minutes<=0 或通道不支持撤单时内部直接跳过）。
            finalized = self._cancel_unfilled_orders(db)
            db.commit()
            # 批次1 3a：对刚收敛 canceled 的超时单做有限追价重下（次数上限兜底）
            if finalized:
                self._requote_after_timeout_cancel(db, finalized)
                db.commit()

    def _requote_after_timeout_cancel(self, db: Session, finalized: list) -> None:
        """批次1 3a：撤单收敛 canceled 后的有限追价重下（真机安全约束）。

        对本轮经超时撤单收敛为 canceled 的单（_cancel_unfilled_orders 返回，
        排除 GUI 手撤/53-54 终态同步路径——仅批次 A 超时撤单挂点）：
        - 目标策略 requote_enabled 且 attempts < requote_max_attempts → 构造
          追价 OrderEvent 重下剩余量（quantity = 原量 − 已成交量，=0 不重下；
          price = 原价 ± tick_size，BUY +tick / SELL −tick；涨跌停界内不加校验，
          ENTER 后 THS/TDX 柜台校验兜底）；
        - 重下单 signal_bar_date 置 None（当 bar 走 _submit_one_order 同款提交链，
          不进 deferred 闸门）；
        - 次数记账 self._requote_attempts[(strategy_id, stock_code, signal_name,
          bar_date)]：重下单 attempts = 旧 n + 1（重启清零可接受——上限兜底）；
        - 非 requote 策略 / 超次数 → 维持现行为（终态 canceled），不重下。
        调用点持 _book_lock（pending_orders 增删与 deals 池串行）。
        """
        for lo in finalized:
            if lo.status != "canceled":
                continue  # 残余成交把单补齐（filled）等：非撤单终态不重下
            portfolio = next(
                (p for p in self.portfolios
                 if p.portfolio_id == lo.portfolio_strategy_id),
                None,
            )
            if portfolio is None:
                continue
            ctx = portfolio.find_strategy(lo.strategy_id)
            if ctx is None or not getattr(ctx, "requote_enabled", False):
                continue
            remaining = int(lo.quantity or 0) - int(lo.filled_quantity or 0)
            if remaining <= 0:
                continue  # 已全部成交（或零量）：无可追
            if lo.signal_type is None:
                # order_id() 需要 signal_type.value；历史 NULL 单跳过重下（保守）
                logger.warning(
                    "requote skip: order %s has NULL signal_type (legacy row)",
                    lo.id,
                )
                continue
            key = (
                lo.strategy_id, lo.stock_code, lo.signal_name or "",
                lo.bar_time.date() if lo.bar_time else None,
            )
            attempts = self._requote_attempts.get(key, 0)
            max_attempts = int(getattr(ctx, "requote_max_attempts", 2) or 0)
            if attempts >= max_attempts:
                logger.info(
                    "requote skip: order %s %s attempts %d >= max %d (session %s)",
                    lo.id, lo.stock_code, attempts, max_attempts, self.session_id,
                )
                continue
            step = tick_size(lo.stock_code)
            orig_price = Decimal(str(lo.price)) if lo.price is not None else None
            if orig_price is None:
                continue  # 无原价依据，不盲目追价
            price = (orig_price + step if (lo.trade_type or "").lower() == "buy"
                     else orig_price - step)
            requote = OrderEvent(
                strategy_id=lo.strategy_id,
                portfolio_id=lo.portfolio_strategy_id,
                stock_code=lo.stock_code,
                trade_type=TradeType(lo.trade_type.upper()),
                signal_type=SignalType(lo.signal_type),
                quantity=remaining,
                price=price,
                bar_time=lo.bar_time,
                signal_name=lo.signal_name or "",
                signal_bar_date=None,  # 追价单当 bar 提交，不进 deferred 闸门
            )
            self._requote_attempts[key] = attempts + 1
            self._rec_live(
                "requote", "submit", portfolio, requote,
                message="撤单追价重下：第 %d/%d 次，%s %s 追价后价格 %s、数量 %d"
                        % (attempts + 1, max_attempts, requote.stock_code,
                           requote.trade_type.value, price, remaining),
            )
            logger.info(
                "requote: order %s timeout-canceled → resubmit %s %s qty=%s "
                "price=%s attempt %d/%d (session %s)",
                lo.id, requote.trade_type.value, requote.stock_code,
                remaining, price, attempts + 1, max_attempts, self.session_id,
            )
            # 重下走与 _handle_bar 相同的提交闸门链（emit_signal=False：非新信号）
            self._submit_one_order(db, portfolio, requote, ctx.period, emit_signal=False)
            self._drain_decisions(db, portfolio)

    async def _deals_loop(self) -> None:
        """G5：/deals 成交回报回填的独立轮询（deals_poll_interval，默认 5s）。

        成交秒级回报，独立短节拍让 LiveTrade/持仓/资金反馈近实时；主循环保持 60s 拉 bar。
        I-8（audit）：tick 投独立单 worker 池（_deals_executor）——主循环拉全池行情
        50-57s/轮时 deals 不再被饿死（原共享单池，deals tick 要排队等主 tick）。
        共享资源（TQ 连接）由 get_tdx_lock 串行化；I-8/C2（终审）：两池真并发，
        跨池记账（backfill/apply、pending_orders 增删）由引擎级 _book_lock 在调用点
        串行化（_tick_deals 持锁调 _poll_deals）。_poll_deals 内部已兜底桥离线/查失败。
        """
        while self._running:
            try:
                await asyncio.get_running_loop().run_in_executor(
                    self._deals_executor, self._tick_deals
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("deals loop executor error")
            await asyncio.sleep(self._deals_poll_interval)

    def _tick_deals(self) -> None:
        """_deals_loop 单轮同步体（在 worker 线程执行）：查未完结单 → 回填 /deals。

        异常在此捕获（非 CancelledError）——与原 _deals_loop 行为一致：记日志不外抛。
        C2（终审）：poll_deals 的回填（backfill → apply / pending 增删）与主池的超时
        收割、收盘清扫真并发，整段记账持 _book_lock 串行化。
        """
        try:
            with self._book_lock:
                self._consume_execution_receipts()
                self._poll_deals()
        except Exception:  # noqa: BLE001
            logger.exception("deals loop unexpected error")

    def _consume_execution_receipts(self) -> None:
        """执行回执消费（file 交接层执行器拒单 → 工作台立即可见）。

        执行器 rejected 回执经 ``dispatcher.execution_receipts()`` 弹出（单消费者
        =引擎，deals 轮询点、记账锁内）：按 order_id（=确定性 bridge_order_id）匹配
        在途 submitted 单 → 走既有 rejected 终态（error_message=回执 error + order
        事件，形状同 expire 收口）；order_id 缺失/匹配不上时回退按 entry 形状
        （stock_code+direction+quantity）匹配无 order_ref 的在途单。匹配不上 →
        记 warning 丢弃（该单由 180s expire 兜底，不阻塞后续回执）；空回执零开销。
        通道可选能力：dispatcher 未实现 execution_receipts（duck-typed fake/旧通道）
        视为无回执，直接跳过（与 close/cancel_order 的 getattr 模式一致）。
        """
        receipts_fn = getattr(self._dispatcher, "execution_receipts", None)
        receipts = receipts_fn() if callable(receipts_fn) else []
        if not receipts:
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
            for r in receipts:
                lo = self._match_receipt_order(pending, r)
                if lo is None:
                    logger.warning(
                        "execution receipt unmatched, dropped (expire fallback): "
                        "order_id=%s error=%s", r.get("order_id"), r.get("error"))
                    continue
                lo.status = "rejected"
                lo.error_message = str(r.get("error") or "executor rejected")
                self._pending_orders.pop(lo.id, None)
                logger.warning(
                    "execution receipt: order %s %s %s rejected by executor (%s)",
                    lo.id, lo.trade_type, lo.stock_code, lo.error_message,
                )
                self._emit("order", {
                    "portfolio_id": lo.portfolio_strategy_id,
                    "order_id": lo.id,
                    "status": "rejected",
                    "stock_code": lo.stock_code,
                    "trade_type": (lo.trade_type or "").upper(),
                    "quantity": lo.quantity,
                    "bar_time": lo.bar_time.isoformat() if lo.bar_time else None,
                    "error_message": lo.error_message,
                })
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _match_receipt_order(pending: list, receipt: dict) -> Optional["LiveOrder"]:
        """回执 → 在途单匹配：order_id（=bridge_order_id）精确优先，
        entry 形状（stock_code+direction+quantity）回退；仅 submitted 且无 order_ref。"""
        oid = str(receipt.get("order_id") or "")
        if oid:
            for lo in pending:
                if (lo.status == "submitted" and lo.order_ref is None
                        and lo.bridge_order_id == oid):
                    return lo
        entry = receipt.get("entry") or {}
        code = str(entry.get("stock_code") or "")
        direction = str(entry.get("direction") or "").upper()
        volume = int(entry.get("quantity") or 0)
        if not code or not direction:
            return None
        for lo in pending:
            if lo.status != "submitted" or lo.order_ref is not None:
                continue
            if str(lo.stock_code or "") != code:
                continue
            if (lo.trade_type or "").upper() != direction:
                continue
            if volume and int(lo.quantity or 0) != volume:
                continue
            return lo
        return None

    # ---------------- 日终/收盘时点编排（0010 步骤 5 转 DailyCloser）----------------
    # 14:30 日终估值/日线驱动、15:05 收盘清扫的"时点判断 + 编排"已迁入
    # core.engine.live.daily_closer.DailyCloser。下列 _maybe_* 保留为薄委托（_tick_main
    # 与测试直连 engine._maybe_*(now=...) 穿透）；三个幂等日期标记经 property 委托到
    # DailyCloser，测试对 engine._last_daily_date 等的直接读写穿透到同一份状态。
    def _maybe_daily_close(self, now: Optional[datetime] = None) -> None:
        self._closer.maybe_daily_close(now)

    def _maybe_daily_bars(self, now: Optional[datetime] = None) -> None:
        self._closer.maybe_daily_bars(now)

    def _maybe_close_sweep(self, now: Optional[datetime] = None) -> None:
        """15:05 收盘清扫（薄委托 → DailyCloser）。

        C2（终审）：清扫的回填/apply/终态收敛与 deals 池回填真并发，
        整段持 _book_lock 串行化（清扫内部含自己的 session 与 commit）。
        """
        with self._book_lock:
            self._closer.maybe_close_sweep(now)

    # ---------------- bar 驱动 ----------------
    def _on_bar(self, bar: BarEvent) -> None:
        """BarPoller 回调（1m 节拍）：① 驱动 1m 策略；② 按 bar stime 边界分发长周期。

        边界判定只读 1m bar stime（periods_on_boundary），不引入本机时钟；
        5m/15m/30m/1h 策略在边界时点才被驱动（C6(A)），1m 节拍不再每 bar 算长周期。
        C4(#28)：df_cache/raw_cache 建在此（跨组合共享）——同 (code,period) 只 query_quote
        一次、同 (code,period,formula) 只算一次（TQ 计算最贵）；同组 (formula,period) 的
        多 code 再合并成一次 compute_injected_batch（N set + 1 process，治 1m 全池节拍）。
        BarPoller 透传的本轮 bars（bar.bars_by_code）直接给 _handle_bar 注入复用——
        1m 判完成与算公式共用一次拉取，消除双拉（BarPoller 已拉 count=10，注入不再增量重拉）。
        """
        df_cache: Dict = {}
        raw_cache: Dict = {}
        bars_by_code = getattr(bar, "bars_by_code", None) or None
        handler = (
            self._handle_alert_bar
            if self.execution_mode == "alert" else self._handle_bar
        )
        for portfolio in self.portfolios:
            try:
                handler(
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

    def _maybe_update_regime(self, portfolio: Portfolio, bar_date: date) -> None:
        """B5：每日首 bar 更新组合市场状态（纯因果——只吸收 ≤当日的基准收盘）。

        跳过条件：provider 未注入 / 组合无启用 regime 的策略（结果缓存于 ctx）/
        当日已更新 / 无 benchmark_code。provider 异常时告警并沿用旧标签（不中断 bar）。
        """
        if self._regime_provider is None:
            return
        if self._regime_updated.get(portfolio.portfolio_id) == bar_date:
            return
        enabled = getattr(portfolio, "_regime_any_enabled", None)
        if enabled is None:
            enabled = any(c.regime_filter_enabled for c in portfolio.strategies)
            portfolio._regime_any_enabled = enabled
        if not enabled:
            return
        code = self._benchmark_codes.get(portfolio.portfolio_id)
        if not code:
            return
        try:
            series = self._regime_provider(code, bar_date)  # {date: Decimal} 全量历史
            tracker = CausalRegimeTracker()
            tracker.feed_history(series)
            portfolio.current_regime = tracker.label
            self._regime_updated[portfolio.portfolio_id] = bar_date
        except Exception:
            logger.warning(
                "portfolio %s regime 更新失败（%s），沿用旧标签",
                portfolio.portfolio_id, code, exc_info=True,
            )

    def _tick_alert(self, now: Optional[datetime] = None) -> None:
        """alert 单轮：TQ 心跳 → 拉行情 → 调度信号；不碰委托/成交/持仓。"""
        was_online = self._bridge_online
        if not self._dispatcher.heartbeat():
            self._bridge_online = False
            return
        self._bridge_online = True
        if not was_online:
            # 离线恢复：丢弃离线期间积压的 bar/预热缓存，避免恢复首轮补发陈旧预警。
            if hasattr(self._bar_poller, "reset_baseline"):
                self._bar_poller.reset_baseline()
            self._preheat_cache.clear()
        self._bar_poller.poll()
        now = now or now_shanghai()
        if self._signal_scheduler is not None:
            for batch in self._signal_scheduler.due(now):
                self._signal_scheduler.execute(batch)

    def _handle_alert_bar(
        self,
        portfolio,
        bar,
        bars_by_code=None,
        df_cache=None,
        raw_cache=None,
    ) -> None:
        """alert 模式：只算公式 + 监控信号，不下单、不跑风控。"""
        self.market_data.fill_signal_cache(
            portfolio,
            bar,
            bars_by_code=bars_by_code,
            df_cache=df_cache,
            raw_cache=raw_cache,
        )
        if self._signal_monitor is not None:
            self._signal_monitor.evaluate(
                portfolio,
                bar,
                self.market_data.signal_cache,
                strategies=portfolio.strategies,
                frequency=bar.period or "1m",
            )

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
        # I-5（audit）：提交链入口拒单闸（deferred 放行与正常单共用此入口）——stop
        # 已请求后不再发起新单。竞态窗口：stop() cancel 任务后，worker 线程在途的
        # 最后一轮 tick 仍可能带着 bar 走进来。
        if self._stop_requested:
            return
        # trading_session（full/am/pm）组合时段闸（历史死配置首次接线）：非允许
        # 时段整轮跳过本组合（风控/信号/下单均不执行，与回测跳过 on_bar 同语义）。
        # 实盘按墙钟判定；时段约束仅对日内周期生效——日线及以上由 14:30 closer
        # 驱动（am 组合的日线策略若被闸过滤会永久卡死，session_allows_bar 周期判定）。
        # full=不筛（既有全局闸已管）。
        # 跳过时每日首次记决策事件（gate=trading_session、layer=portfolio）防刷屏。
        session = getattr(portfolio, "trading_session", None) or "full"
        if session in ("am", "pm") and not session_allows_bar(
            session, now_shanghai().time(), bar.period
        ):
            key = (portfolio.portfolio_id, bar.bar_time.date())
            if key not in self._session_skip_recorded:
                self._session_skip_recorded.add(key)
                self._recorder.record(
                    gate="trading_session", layer="portfolio", action="skip",
                    portfolio_id=portfolio.portfolio_id,
                    strategy_id=None, stock_code=None, bar_time=bar.bar_time,
                    message="交易时段闸：%s 时段组合当前 %s 不在允许窗口（%s），本轮跳过"
                            % (session, now_shanghai().strftime("%H:%M:%S"),
                               "09:30–11:30" if session == "am" else "13:00–15:00"),
                )
            db = self._db_session_factory()
            try:
                self._drain_decisions(db, portfolio)
            finally:
                db.close()
            return
        # E5：每 bar 更新峰值/回撤（分钟级 prev_close 只在跨日刷新，不误触 daily_loss）
        total_value = portfolio.total_value(bar)
        # R3：每日首根 bar 补算上一日 daily_loss（与回测 enter_day 对齐，镜像回测时序——
        # 必须在 on_bar_update 之前调用：后者会把 _last_bar_date 置为当日，enter_day
        # 的跨日判断将失效）。日内亏损暂停/次日恢复在日首即生效，14:30 closer 的
        # update_daily 仍做当日收尾。首根 bar（无上一日数据）不调用，行为不变。
        if self._enter_day_updated.get(portfolio.portfolio_id) != bar.bar_time.date():
            portfolio.risk_manager.enter_day(
                bar.bar_time.date(), portfolio.account.initial_capital
            )
            self._enter_day_updated[portfolio.portfolio_id] = bar.bar_time.date()
        # update_peak + max_drawdown 次日自动恢复检测 + H4 计数持久化归 BreakerService（0010 步骤 4）
        self._breaker.on_bar_update(portfolio, total_value, bar.bar_time.date())
        # F5：每 bar 刷一次桥可用持仓（多组合共享同一 bar 对象 → 强引用去重），
        # SELL 减仓上限用 m_nCanUseVolume（T+1 可用），供 cap_quantity 取数。
        if self._available_bar is not bar:
            self._available_bar = bar
            self._refresh_available_map()
        self._fill_signal_cache(
            portfolio, bar, bars_by_code=bars_by_code,
            df_cache=df_cache, raw_cache=raw_cache,
        )
        if self._signal_monitor is not None:
            self._signal_monitor.evaluate(
                portfolio,
                bar,
                self.signal_cache,
                strategies=portfolio.strategies,
                frequency=bar.period or "1m",
            )
        # C6：period 过滤——5m 边界 bar 不触发 1m 策略的风控单（_check_risks 读 bar.stocks close）
        # 在途单注入（OPEN 闸门 × max_positions）：_pending_orders 里的未成交 BUY
        # （submitted/partial）尚未落持仓（submitted 只建 quantity=0 的 Position），
        # 闸门 held 只数已成交——不注入则连续 bar 齐发 OPEN 超开/同票重复开仓。
        # partial 未 apply（只记账），整单仍在途，计入正确。
        # C2（终审）：跨池（deals 回填/超时收割）会并发 pop/替换 _pending_orders，
        # 遍历段持 _book_lock 防 dict changed size during iteration。
        inflight_opens: Dict[int, set] = {}
        with self._book_lock:
            for lo in self._pending_orders.values():
                if lo.trade_type == "buy":
                    inflight_opens.setdefault(lo.strategy_id, set()).add(lo.stock_code)
        # B5：每日首 bar 更新组合市场状态（未启用策略/provider 缺省 → 跳过，行为不变）
        self._maybe_update_regime(portfolio, bar.bar_time.date())
        # B-1 未来函数闸门放行：跨日后释放 deferred 的日线信号订单（bar_time 更新为
        # 当前 bar——决策时间戳语义），与 on_bar 新单合并重走同一提交链
        # （approve/收盘/日历/去重/在途门全复用，且放行单先行——对齐回测
        # 「上一 bar pending 先于本 bar 新信号成交」时序）。
        released = self._release_signal_bar_deferred(portfolio, bar)
        orders = portfolio.on_bar(
            bar, signal_cache=self.signal_cache, period=bar.period,
            inflight_opens=inflight_opens or None,
        )
        if released:
            orders = list(released) + list(orders or [])
        # db 会话在 on_bar 之后即开：决策闸门事件（熔断 halt/剥 BUY/风险触发）可能在无新单
        # 时也产生，需随本 bar 落库——故不再 orders 为空就早退，drain 放 finally 收尾。
        db = self._db_session_factory()
        try:
            for order in (orders or []):
                # 单笔订单走完整提交闸门链（撤单追价重下共用同一提交链，批次1 3a）
                self._submit_one_order(db, portfolio, order, bar.period)
            # 决策闸门事件随本 bar 统一落库 + SSE 推送（含 orders 为空时的 halt/strip 事件）。
            self._drain_decisions(db, portfolio)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _submit_one_order(
        self,
        db: Session,
        portfolio: Portfolio,
        order: OrderEvent,
        period: Optional[str],
        emit_signal: bool = True,
    ) -> None:
        """提交单笔订单走完整闸门链（_handle_bar 与撤单追价重下共用，批次1 3a）。

        闸门序（与原 _handle_bar 内联段逐字等价，continue→return）：
        1w/1mon signal SSE → 收盘后守卫 → 交易日历总闸 → B-1 未来函数 deferred
        （risk_timing=immediate 跳过）→ 资金/T+1 审批 → 跨重启去重（D6）→
        在途单门（F7）→ 落 submitted → 发桥（桥离线/拒单收口）。
        闸门拦截时静默返回（决策事件已记录）；异常向上抛由调用方回滚。
        """
        ctx = portfolio.find_strategy(order.strategy_id)
        if ctx is None:
            return
        # 1w/1mon 的 signal_cache 由注入路径提供、monitor 不重算，
        # 保留旧成单 SSE 避免前端丢信号；其余周期由 monitor 统一发。
        # 追价重下单非新信号（emit_signal=False），不重复推。
        if emit_signal and (self._signal_monitor is None or period in ("1w", "1mon")):
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
            self._rec_live(
                "after_close_block", "block", portfolio, order,
                message="bar_time %s >= 15:00 收盘后不下单" % bt,
            )
            return
        # 交易日历总闸：非交易日不下新单。用 bar_time（非墙钟，见上方收盘守卫同理）。
        # 桥日历时 fail-open（工作日放行），只在权威日历明确判定"非交易"时才拦——
        # 周末/节假日绝不误下，真实交易日不误挡。风控 update_peak 与 signal 事件仍
        # 照常执行（只拦落单）。
        # 日内周期(1m/5m...)额外卡交易时段 09:30–15:00；1d/1w/1mon 的 bar_time 按
        # 约定是当日 00:00（由 14:30 _maybe_daily_bars 统一驱动），不套时段门，
        # 只判交易日，否则会把 14:30 合法驱动的日线单误杀。
        if bt is not None and not self._trading_allowed_for(bt, period):
            logger.info(
                "skip non-trading order %s %s %s bar=%s period=%s (calendar/closed)",
                order.trade_type.value, order.stock_code, order.signal_name,
                bt, period,
            )
            self._rec_live(
                "non_trading_block", "block", portfolio, order,
                message="非交易日/非交易时段 period=%s bar=%s" % (period, bt),
            )
            return
        # B-1 未来函数闸门（与回测同式判定 t.date() <= signal_bar_date）：
        # 日线级信号（signal_bar_date 非 None，signal bar 为当日 00:00，close
        # 未确认）当日不下单——实盘若 14:30 即真实下单、回测却次日开盘成交，
        # 回测结果对实盘不可预测。订单进 deferred 队列，次一交易日首 bar 放行。
        # 队列仅内存（Core 重启即丢，P2 持久化）；stop() 丢弃属预期。
        # 批次1 risk_timing=immediate（当日确认立即执行）跳过本闸门，
        # 当日直接提交；next_day（默认）维持次日执行。
        if (order.signal_bar_date is not None and order.bar_time is not None
                and order.bar_time.date() <= order.signal_bar_date
                and getattr(ctx, "risk_timing", "next_day") != "immediate"):
            self._signal_bar_deferred.append(order)
            self._rec_live(
                "signal_bar_date", "defer", portfolio, order,
                message="日线级信号次日执行：signal_bar_date=%s，本 bar %s 不下单，"
                        "延至次一交易日首 bar 提交" % (order.signal_bar_date, order.bar_time),
            )
            return
        # BUY 首次建仓：确保 Position 存在（同回测 BacktestEngine；submitted 不 apply）
        pos = ctx.positions.get(order.stock_code)
        if pos is None and order.trade_type == TradeType.BUY:
            pos = Position(order.stock_code)
            ctx.positions[order.stock_code] = pos
        # F5/BUY 量上限：先定最终下单量（账户资金审批 / 桥 T+1 可用）再落
        # submitted——DB 下单量与实发一致，回填不误判 partial。None=不通过不下单。
        # B-2 策略级敞口上限：与回测 _exposure_kwargs 完全同式透传
        # （strategy_position_value/strategy_capital_limit），多策略上限之和
        # 可超 100% 时实盘敞口不再系统性大于回测。
        capped = self._engine.cap_quantity(
            order, portfolio.account, pos,
            **self._exposure_kwargs(portfolio, ctx),
        )
        if capped is None:
            logger.info(
                "skip order %s %s %s: cap_quantity None（资金/持仓上限拦截，不下单）",
                order.trade_type.value, order.stock_code, order.signal_name,
            )
            return
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
                self._rec_live(
                    "dup_skip", "block", portfolio, order,
                    message="同 bar 同向单已存在 order=%s" % dup.id,
                )
                return
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
        # 追价重下单在原单收敛 canceled 之后构造（不在途），天然过门。
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
            self._rec_live(
                "inflight_skip", "block", portfolio, order,
                message="同向在途单未确认 order=%s status=%s" % (inflight.id, inflight.status),
            )
            return
        # ① 先写 submitted + commit（I4 命门窗口闭合）；计入在途集合（G7 计数）
        live_order = self._persist_order_submitted(db, order)
        self._pending_orders[live_order.id] = live_order
        # B5：订单状态推送（submitted——真实成交回报由 _poll_deals 回填后推 filled）
        self._emit("order", {
            "portfolio_id": portfolio.portfolio_id,
            "order_id": live_order.id,
            "strategy_id": live_order.strategy_id,
            "stock_code": live_order.stock_code,
            "trade_type": (live_order.trade_type or "").upper(),
            "status": "submitted",
            "quantity": live_order.quantity,
            "price": float(live_order.price) if live_order.price is not None else None,
            "bar_time": live_order.bar_time.isoformat() if live_order.bar_time else None,
        })
        db.commit()
        # THS file_builtin 入队标记：place_order 秒回 None 但指令已写入
        # 交接文件时非 None（=本单确定性 order_id）。其它通道恒 None。
        enqueued_oid: Optional[str] = None
        try:
            # 涨跌停前置校验（price_limit_check_enabled 配置门控，通道无关，
            # 口径与回测同源）：approve（cap_quantity）之后、place_order 之前。
            # 返回拒绝原因 → raise BridgeOrderRejected 沿用既有拒单路径
            # （rejected 落库 + 决策事件 + SSE 回显原因）。配置关闭 → 完全跳过。
            if portfolio.price_limit_check_enabled:
                reason = self._dispatcher.check_price_limit(
                    order.stock_code,
                    "buy" if order.trade_type == TradeType.BUY else "sell",
                    order.price,
                )
                if reason is not None:
                    raise BridgeOrderRejected(reason)
            # ② 再发 passorder（apply=False：submitted 阶段不更新账户持仓）
            # B-2：execute 内部二次 cap_quantity/approve 同样透传敞口上限
            # （跳空/价格变动后的重批与首审同口径）。
            # Fix4：buy_fee_cfg 透传组合费用口径——execute 发单前做 BUY
            # 费用感知前置钳制（approve 只卡价×量，尾款不足佣金会透支
            # 虚拟现金；桥通道无法发单后改量重发）。
            trade = self._engine.execute(
                order, portfolio.account, pos, apply=False,
                buy_fee_cfg=self._buy_fee_cfg(portfolio),
                **self._exposure_kwargs(portfolio, ctx),
            )
        except BridgeUnavailableError:
            # 桥离线：标 rejected + 置离线暂停（_on_bar 上层心跳循环据此暂停下单）
            live_order.status = "rejected"
            live_order.error_message = "bridge unavailable"
            self._bridge_online = False
            self._rec_live(
                "bridge_unavailable", "reject", portfolio, order,
                message="桥离线（dispatcher 不可达）",
            )
            self._pending_orders.pop(live_order.id, None)
            self._emit("order", {
                "portfolio_id": portfolio.portfolio_id,
                "order_id": live_order.id,
                "status": "rejected",
                "stock_code": live_order.stock_code,
                "trade_type": (live_order.trade_type or "").upper(),
                "quantity": live_order.quantity,
                "bar_time": live_order.bar_time.isoformat() if live_order.bar_time else None,
                "error_message": live_order.error_message,
            })
            db.commit()
            return
        except BridgeOrderRejected as e:
            # 桥业务拒单（白名单/限额/重复）→ rejected，回显桥侧真实原因
            # （此前被吞成笼统 "approval failed"，真机查不了原因）。
            live_order.status = "rejected"
            live_order.error_message = str(e)
            self._rec_live(
                "bridge_rejected", "reject", portfolio, order,
                message="桥业务拒单: %s" % str(e),
            )
            self._pending_orders.pop(live_order.id, None)
            self._emit("order", {
                "portfolio_id": portfolio.portfolio_id,
                "order_id": live_order.id,
                "status": "rejected",
                "stock_code": live_order.stock_code,
                "trade_type": (live_order.trade_type or "").upper(),
                "quantity": live_order.quantity,
                "bar_time": live_order.bar_time.isoformat() if live_order.bar_time else None,
                "error_message": live_order.error_message,
            })
            db.commit()
            return
        if trade is None:
            # THS file_builtin：place_order 已把指令写入交接文件并秒回 None
            # → 本单已受理入队，保持 submitted 等内置执行器执行 + UI 回填
            # 链路确认成交（执行失败单由执行器落 results rejected 回执，
            # 残留 submitted 单由 180s expire 兜底标 rejected）。
            enqueued_oid = getattr(self._dispatcher, "last_enqueued_order_id", None)
            if enqueued_oid and enqueued_oid == self._dispatcher.order_id(order):
                logger.info(
                    "order enqueued to ths handoff: id=%s %s %s qty=%s (session %s)",
                    live_order.id, order.trade_type.value, order.stock_code,
                    live_order.quantity, self.session_id,
                )
            else:
                # 兜底：execute 内部审批不过（前置 cap 与 execute 间账户变化）
                # 或桥返回 {ok:false} 无 error（非 JSON 故障路径，#24）→ rejected
                live_order.status = "rejected"
                live_order.error_message = "approval failed"
                self._rec_live(
                    "approval_failed", "reject", portfolio, order,
                    message="发单兜底失败（审批不过/桥返回 ok:false 无原因）",
                )
                self._pending_orders.pop(live_order.id, None)
                self._emit("order", {
                    "portfolio_id": portfolio.portfolio_id,
                    "order_id": live_order.id,
                    "status": "rejected",
                    "stock_code": live_order.stock_code,
                    "trade_type": (live_order.trade_type or "").upper(),
                    "quantity": live_order.quantity,
                    "bar_time": live_order.bar_time.isoformat() if live_order.bar_time else None,
                    "error_message": live_order.error_message,
                })
                db.commit()
                return
        # ③ 桥受理成功：回写幂等 order_id，尝试同步定位 OrderRef（失败下轮回填再找）
        live_order.bridge_order_id = self._dispatcher.order_id(order)
        # THS file_builtin：指令刚入队、执行器尚未真正下单，立即回查只会
        # 碰到 120s 认领窗内的同形状旧单（误绑 → 成交回填张冠李戴）。
        # ref 留 None，执行器下单后由 /deals 回填周期再定位（彼时本单是
        # 最新同形状行，与 direct 模式同一口径）。
        if enqueued_oid != live_order.bridge_order_id:
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

    def _release_signal_bar_deferred(
        self, portfolio: Portfolio, bar: BarEvent
    ) -> List[OrderEvent]:
        """B-1：跨日放行 deferred 的日线信号订单（引擎级队列，只取本组合的）。

        放行条件与回测闸门同源：order.bar_time.date() < 当前 bar 日期（严格小于
        =已跨日，同日后续 bar 不放行）。放行时把 order.bar_time 更新为当前 bar
        时间（决策时间戳语义），随后重走完整提交链。
        """
        if not self._signal_bar_deferred:
            return []
        if bar.bar_time is None:
            return []
        bar_date = bar.bar_time.date()
        released: List[OrderEvent] = []
        remaining: List[OrderEvent] = []
        for order in self._signal_bar_deferred:
            if (order.portfolio_id == portfolio.portfolio_id
                    and order.bar_time is not None
                    and order.bar_time.date() < bar_date):
                order.bar_time = bar.bar_time
                released.append(order)
            else:
                remaining.append(order)
        self._signal_bar_deferred = remaining
        return released

    def _exposure_kwargs(self, portfolio: Portfolio, ctx) -> dict:
        """B-2 策略级敞口上限（#3，与 BacktestEngine._exposure_kwargs 完全同式）。

        enabled 判定 exposure_cap_enabled；策略持仓市值口径 = portfolio.holdings_value(ctx)
        （该策略虚拟持仓按最近已知价估值，_handle_bar 顶部 total_value(bar) 已刷新
        价格快照，与回测「上一 bar 收盘估值」同口径）；上限 = capital_ratio × 组合
        初始资金。关闭返回空 dict，cap_quantity/execute 走默认 None（行为不变）。
        """
        if not getattr(ctx, "exposure_cap_enabled", False):
            return {}
        return {
            "strategy_position_value": portfolio.holdings_value(ctx),
            "strategy_capital_limit": ctx.capital_ratio * portfolio.account.initial_capital,
        }

    @staticmethod
    def _buy_fee_cfg(portfolio: Portfolio) -> Optional[tuple]:
        """Fix4：组合费用参数 → execute 费用感知钳制口径 (最低佣金, 买入费率)。

        cost_params 缺项（理论不可达，assemble_portfolio 恒全量装配）→ None，
        execute 跳过前置钳制（原行为）。
        """
        cp = getattr(portfolio, "cost_params", None) or {}
        min_comm = cp.get("min_commission")
        rate = cp.get("buy_commission_rate")
        if min_comm is None or rate is None:
            return None
        return (min_comm, rate)

    def _rec_live(
        self, gate: str, action: str, portfolio: Portfolio, order: OrderEvent,
        message: Optional[str] = None,
    ) -> None:
        """记录一条实盘专属闸门事件（收盘/日历/去重/在途/桥拒单）。"""
        self._recorder.record(
            gate=gate, layer="live_gate", action=action,
            portfolio_id=portfolio.portfolio_id,
            strategy_id=order.strategy_id,
            stock_code=order.stock_code,
            bar_time=order.bar_time,
            message=message,
        )

    def _drain_decisions(self, db, portfolio: Portfolio) -> None:
        """把本 bar 缓冲的决策闸门事件落 LiveDecisionEvent + 推 SSE，一次 commit。

        失败不影响交易主链路（仅观测）：回滚本批事件行、记异常，不抛出。
        """
        events = self._recorder.drain()
        if not events:
            return
        try:
            for ev in events:
                db.add(LiveDecisionEvent(
                    live_session_id=self.session_id,
                    gate=ev.gate,
                    layer=ev.layer,
                    action=ev.action,
                    portfolio_id=ev.portfolio_id,
                    strategy_id=ev.strategy_id,
                    stock_code=ev.stock_code,
                    bar_time=ev.bar_time,
                    param_name=ev.param_name,
                    param_value=ev.param_value,
                    actual_value=ev.actual_value,
                    requested_qty=ev.requested_qty,
                    final_qty=ev.final_qty,
                    message=(ev.message or "")[:200],
                ))
                self._emit("decision", {
                    "portfolio_id": ev.portfolio_id,
                    "strategy_id": ev.strategy_id,
                    "stock_code": ev.stock_code,
                    "gate": ev.gate,
                    "layer": ev.layer,
                    "action": ev.action,
                    "param_name": ev.param_name,
                    "param_value": ev.param_value,
                    "actual_value": ev.actual_value,
                    "requested_qty": ev.requested_qty,
                    "final_qty": ev.final_qty,
                    "message": ev.message,
                    "bar_time": ev.bar_time.isoformat() if ev.bar_time else None,
                })
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("drain decision events failed (session %s)", self.session_id)

    def _persist_breaker_count(
        self, portfolio: Portfolio, total_value: Decimal = None
    ) -> None:
        """H4：薄委托 → BreakerService.persist_count（0010 步骤 4）。"""
        self._breaker.persist_count(portfolio, total_value)

    def recover_breaker(self, portfolio_id: int) -> bool:
        """手动恢复某组合熔断（公共方法，薄委托 → BreakerService.recover，0010 步骤 4）。"""
        return self._breaker.recover(portfolio_id)

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

    def _cancel_unfilled_orders(self, db: Session) -> list:
        """A-2（I-4/C-1）：在途单超时未成交 → 真实撤单（薄委托 → OrderStateMachine）。

        返回本轮收敛为 canceled 的单（批次1 3a 追价重下挂点；其余调用方忽略）。
        """
        return self._order_machine.cancel_unfilled_orders(db)

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
        # A-4（C-2）：重启恢复单的 ref 并入 dispatcher 的 _known_refs（A-5 防重放基线），
        # 并按 created_at（UTC naive）换算上海时间补记 _submit_times——通达信委托表
        # 无时间列，重启后不补记则 legacy 窗口匹配（_ORDER_INSERT_TOLERANCE）不可用，
        # 挂回单的成交回填/认领会全部哑火。
        submit_times = getattr(self._dispatcher, "_submit_times", None)
        known_refs = getattr(self._dispatcher, "_known_refs", None)
        claimed_refs = getattr(self._dispatcher, "_claimed_refs", None)
        for lo in pending:
            ref = lo.order_ref
            if not ref:
                continue
            if known_refs is not None:
                known_refs.add(ref)
            # 批次 A 评审：挂回单的 ref 已绑定 DB 单（语义即已认领），必须同时进
            # _claimed_refs——否则重启后 _claimed_refs 为空，新单 fallback
            # 「形状匹配且未 claimed」可能绑到已恢复单的 ref，deals 按 ref 双份回填。
            if claimed_refs is not None:
                claimed_refs.add(ref)
            if submit_times is not None and ref not in submit_times:
                if lo.created_at is not None:
                    sh = lo.created_at + timedelta(hours=8)  # UTC naive → 上海 naive
                else:
                    sh = now_shanghai()
                submit_times[ref] = (sh.strftime("%Y%m%d"), sh.strftime("%H%M%S"))
        if pending:
            logger.info(
                "recover: %d pending live orders to backfill (session %s)",
                len(pending), self.session_id,
            )
        # D4：读回熔断计数（重启不丢累计次数，达 3 次转手动恢复）归 BreakerService
        # （0010 步骤 4）；预置 counts_written 避免首 bar 重复落库。
        self._breaker.restore_counts(db, ports_by_id)
        # D3：虚拟持仓 vs 桥实际 /positions 对账（仅告警不修正，见 _reconcile_positions）
        self._reconcile_positions()

    def _reconcile_positions(self) -> None:
        """D3：对账——虚拟持仓(按 code 聚合) vs 桥实际 /positions，不一致仅记录+告警。

        recover 重建的虚拟持仓以 live_trades 为唯一源；桥 /positions 是账户真实持仓。
        比对口径：按 stock_code 聚合所有组合策略虚拟净持仓 vs 桥 volume。
        差异记 `_reconcile_mismatches` + 告警日志 + risk SSE 事件（I-7：工作台事件
        日志立即可见，无需翻 Core 日志），**不自动修正账面**（首期安全：
        在途单未回填/桥行情延迟期可能假不一致，自动修正反而引入错账；真机跑顺后
        如需自动校准再放开）。桥离线 → 记日志跳过（保留上次差异结果，不闪空）。
        附加提示：实际有仓但虚拟无仓（如 Core 宕机期手动下单）也记录，供人工核对。
        """
        try:
            rows = self._dispatcher.query_positions()
        except BridgeUnavailableError:
            logger.warning("reconcile skipped: bridge offline (session %s)", self.session_id)
            return
        # 查询成功才清空旧差异：桥离线跳过时保留上次结果（API 可见性不闪空）
        self._reconcile_mismatches = []
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
            # I-7（audit）：差异推 risk 事件（形状照既有 risk：rule + message）
            self._emit("risk", {
                "rule": "reconcile_mismatch",
                "stock_code": code,
                "virtual": v,
                "real": r,
                "diff": v - r,
                "message": "持仓对账差异 %s：虚拟=%d 桥实际=%d 差=%d" % (code, v, r, v - r),
            })
            logger.warning(
                "reconcile mismatch (session %s) %s: virtual=%d real=%d diff=%d",
                self.session_id, code, v, r, v - r,
            )

    def _maybe_reconcile_after_close(self, now: Optional[datetime] = None) -> None:
        """I-7（audit）：每日收盘清扫完成后复查一次对账（幂等，每交易日一次）。

        挂点在 _tick_main 的 _maybe_close_sweep 之后：此时在途单已终态收敛、
        成交已回填，对账差异最可信。用 closer 的 last_close_sweep_date 判定
        「清扫已完成」（deferred 重试轮不置位 → 顺延到清扫真正完成的那轮）。
        """
        today = (now or now_shanghai()).date()
        if self._last_reconcile_date == today:
            return
        if self._last_close_sweep_date != today:
            return
        self._last_reconcile_date = today
        self._reconcile_positions()

    @property
    def reconcile_mismatches(self) -> List[dict]:
        """I-7：最近一次对账差异摘要（[] = 无差异/未对账），session API 序列化用。"""
        return self._reconcile_mismatches

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
    def dispatcher(self):
        """暴露 dispatcher 供桥状态查询等只读访问。"""
        return self._dispatcher
