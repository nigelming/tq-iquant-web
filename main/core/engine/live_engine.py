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
from datetime import date, datetime
from decimal import Decimal
from typing import Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from .portfolio import Portfolio
from .strategy_context import StrategyContext
from .position import Position
from .execution_engine import ExecutionEngine, LiveT1Checker
from .event import BarEvent, OrderEvent, TradeEvent
from .http_bridge_dispatcher import HttpBridgeDispatcher, BridgeUnavailableError
from .bar_poller import BarPoller, parse_bar_time, to_ohlcv, latest_completed_bar
from core.models import LiveOrder, LiveTrade
from core.tq.formula import TQFormula
from tq_iquant_shared.constants import SignalType, TradeType

logger = logging.getLogger(__name__)

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
            import math
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
        poll_interval: float = 30.0,
        tq_formula: Optional[TQFormula] = None,
        formula_by_strategy: Optional[Dict[int, str]] = None,
        formula_count: int = 200,
    ):
        self.session_id = session_id
        self.portfolios = portfolios
        self._dispatcher = dispatcher
        self._bar_poller = bar_poller
        self._db_session_factory = db_session_factory
        self._poll_interval = poll_interval
        # 实盘执行引擎：复用回测 ExecutionEngine，注入桥 dispatcher + 实盘 T+1 检查
        self._engine = ExecutionEngine(dispatcher, LiveT1Checker())

        # 公式注入（0010）：tq_formula 封装内存注入链路；formula_by_strategy 预加载
        # {strategy_id: formula_name}，避免每 bar 查库；formula_count 为注入历史根数
        # （1m/5m 默认 200，够均线预热；不足时调大）。
        self._tq_formula = tq_formula
        self._formula_by_strategy: Dict[int, str] = formula_by_strategy or {}
        self._formula_count = formula_count

        # 信号缓存：(strategy_id, stock_code, bar_time) -> [{"name": str, "value": int}]
        # 风控信号（止损/止盈/移动止损）由 Portfolio._check_risks 直接生成，无需缓存；
        # 公式信号（OPEN/ADD/REDUCE/CLOSE）需缓存命中才触发——_fill_signal_cache 在
        # 每根 bar 前拉历史 → 内存注入算公式 → 填此 dict。测试可直接预置以验证下单链路。
        self.signal_cache: Dict = {}

        self._running = False
        self._task: Optional[asyncio.Task] = None
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

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        """起 asyncio 循环任务，绑定 BarPoller.on_bar 回调。"""
        if self._running:
            return
        self._running = True
        # C6(C)：启动时通达信注入 1w/1mon 策略信号（桥端 xtdata 拉不到，仅此通路）。
        # 一次同步 TDX 计算（get_tdx_lock 串行），阻塞事件循环可接受（启动一次性）。
        self._inject_startup_periods(
            datetime.combine(datetime.now().date(), datetime.min.time())
        )
        self._bar_poller.on_bar = self._on_bar
        self._task = asyncio.create_task(self._loop())

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

    async def _loop(self) -> None:
        """主循环：心跳 → 拉 bar → 日终 → 回填 → sleep。桥离线则暂停下单、标状态，不抛异常。"""
        while self._running:
            try:
                # E8：心跳前的在线状态，用于离线→在线转场时重建基线
                was_online = self._bridge_online
                if not self._dispatcher.heartbeat():
                    self._bridge_online = False
                    logger.warning("bridge offline, pause trading (session %s)", self.session_id)
                    await asyncio.sleep(self._poll_interval)
                    continue
                self._bridge_online = True
                if not was_online:
                    # E8：离线恢复 → 重建基线，跳过离线期间错过的 bar（不补触发）
                    self._bar_poller.reset_baseline()
                    logger.info(
                        "bridge back online, reset poller baseline (session %s)",
                        self.session_id,
                    )
                # poll() 内部对每根完成的 bar 触发 self._on_bar 回调
                self._bar_poller.poll()
                # E5/E6：14:30 日终一次 update_daily（日内亏损/熔断次日恢复推进）
                self._maybe_daily_close()
                # C6(B/C)：14:30 日终一次 1d 快照 bar + 1w/1mon 通达信注入驱动
                self._maybe_daily_bars()
                # 切片5 G2：每轮查未完结 LiveOrder → 轮询桥 /deals 回填真实成交
                self._poll_deals()
            except BridgeUnavailableError as e:
                self._bridge_online = False
                logger.warning("bridge unavailable: %s, skip this round", e)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("live loop unexpected error")
            await asyncio.sleep(self._poll_interval)

    # ---------------- 日终（E5/E6）----------------
    def _maybe_daily_close(self) -> None:
        """本机时间 ≥ 14:30 且当日未算过 → 对每个组合调一次 update_daily。

        日终一次：日内盈亏 daily_pnl = 当前总市值 - prev_close（昨日收盘，update_peak 跨日刷新），
        检测 daily_loss 暂停 + 次日恢复。用本机 Asia/Shanghai 时钟（实盘固有时点，同 C6 1d 快照时点）。
        幂等：_last_daily_date 记录当日已算，避免每轮循环重复触发。
        """
        now = datetime.now()
        if (now.hour, now.minute) < (14, 30):
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
                portfolio.risk_manager.update_daily(
                    total, today, portfolio.account.initial_capital
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "update_daily error (portfolio %s, date %s)",
                    portfolio.portfolio_id, today,
                )

    def _maybe_daily_bars(self, now: Optional[datetime] = None) -> None:
        """C6(B/C)：日终（≥14:30 当日一次）1d 快照 bar + 1w/1mon 通达信注入驱动。

        C6(B) 1d：拉桥 /quote?period=1d 最新 forming 1d bar → 构造 BarEvent(period="1d")
          → _fill_signal_cache 注入（period="1d"）→ on_bar 驱动 1d 策略。
        C6(C) 1w/1mon：走 TQFormula.compute 通达信自取（桥端 xtdata 拉不到），信号预填
          signal_cache[(sid, code, daily_time)]，此处驱动命中预填信号。
        幂等：_last_daily_bar_date 记录当日已触发。日切时（新 daily_time 的 1w/1mon
        cache miss）→ 通达信补注入。用本机 Asia/Shanghai 时钟（实盘固有时点，同 E5/E6）。
        """
        if now is None:
            now = datetime.now()
        if (now.hour, now.minute) < (14, 30):
            return
        today = now.date()
        if self._last_daily_bar_date == today:
            return
        # 拉 1d 快照（供 1d 注入 + 构造 daily_time/stocks）
        bars_by_code: Dict[str, list] = {}
        for code in self._bar_poller._stock_codes:
            try:
                bars = self._dispatcher.query_quote(
                    code, period="1d", count=self._formula_count
                )
            except BridgeUnavailableError:
                self._bridge_online = False
                logger.warning("bridge offline on daily bars (session %s)", self.session_id)
                return
            if bars:
                bars_by_code[code] = bars
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
        for period in ("1d", "1w", "1mon"):
            bar_event = BarEvent(stocks=stocks, bar_time=daily_time, period=period)
            for portfolio in self.portfolios:
                try:
                    self._handle_bar(portfolio, bar_event, bars_by_code=bars_by_code)
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
        """
        for portfolio in self.portfolios:
            try:
                self._handle_bar(portfolio, bar)
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
        # C6(A)：1m bar 边界 → 分发 5m/15m/30m/1h（可累积）
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

    def _handle_bar(
        self, portfolio: Portfolio, bar: BarEvent, bars_by_code: Optional[Dict[str, list]] = None
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
        portfolio.risk_manager.update_peak(self._total_value(portfolio, bar), bar.bar_time.date())
        self._fill_signal_cache(portfolio, bar, bars_by_code=bars_by_code)
        # C6：period 过滤——5m 边界 bar 不触发 1m 策略的风控单（_check_risks 读 bar.stocks close）
        orders = portfolio.on_bar(bar, signal_cache=self.signal_cache, period=bar.period)
        if not orders:
            return
        db = self._db_session_factory()
        try:
            for order in orders:
                ctx = self._find_strategy(portfolio, order.strategy_id)
                if ctx is None:
                    continue
                # BUY 首次建仓：确保 Position 存在（同回测 BacktestEngine；submitted 不 apply）
                pos = ctx.positions.get(order.stock_code)
                if pos is None and order.trade_type == TradeType.BUY:
                    pos = Position(order.stock_code)
                    ctx.positions[order.stock_code] = pos
                # ① 先写 submitted + commit（I4 命门窗口闭合）；计入在途集合（G7 计数）
                live_order = self._persist_order_submitted(db, order)
                self._pending_orders[live_order.id] = live_order
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
                    db.commit()
                    continue
                if trade is None:
                    # 审批不过 / 桥业务拒绝 → rejected（不成交，不 apply）
                    live_order.status = "rejected"
                    live_order.error_message = "approval failed or bridge rejected"
                    self._pending_orders.pop(live_order.id, None)
                    db.commit()
                    continue
                # ③ 桥受理成功：回写幂等 order_id，尝试同步定位 OrderRef（失败下轮回填再找）
                live_order.bridge_order_id = self._dispatcher._order_id(order)
                self._try_match_order_ref(live_order)
                db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _dispatch_period_bar(self, period: str, boundary_time: datetime) -> None:
        """C6(A) 边界分发：period 边界到（1m stime 判定）→ 拉该周期 bar → 注入 → 驱动该周期策略。

        对每 code 拉 query_quote(period, count=formula_count) 一次，既供公式注入又供 BarEvent。
        取每 code「最新已完成 bar」（stime < 本批 latest）的 OHLCV 构造 BarEvent
        （bar_time=boundary_time，与 1m 节拍对齐）——不用 forming 最新一根（未来函数）。
        桥拉取抛 BridgeUnavailableError → 向上传播由 _on_bar 置离线。无完成 bar 的 code 跳过。
        """
        bars_by_code: Dict[str, list] = {}
        stocks: Dict[str, dict] = {}
        for code in self._bar_poller._stock_codes:
            bars = self._dispatcher.query_quote(
                code, period=period, count=self._formula_count
            )
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
        for portfolio in self.portfolios:
            self._handle_bar(portfolio, bar_event, bars_by_code=bars_by_code)

    @staticmethod
    def _total_value(portfolio: Portfolio, bar: BarEvent) -> Decimal:
        """组合总市值 = 现金 + 所有策略持仓按当前 close 的市值。同回测 _total_value。"""
        total = portfolio.account.cash
        for ctx in portfolio.strategies:
            for stock_code, pos in ctx.positions.items():
                if pos.quantity == 0 or stock_code not in bar.stocks:
                    continue
                close = bar.stocks[stock_code]["close"]
                total += close * pos.quantity
        return total

    @staticmethod
    def _find_strategy(portfolio: Portfolio, strategy_id: int) -> Optional[StrategyContext]:
        for ctx in portfolio.strategies:
            if ctx.strategy_id == strategy_id:
                return ctx
        return None

    # ---------------- 公式信号注入（0010）----------------
    def _fill_signal_cache(
        self, portfolio: Portfolio, bar: BarEvent, bars_by_code: Optional[Dict[str, list]] = None
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
        无 tq_formula / 策略无公式映射 / 拉取为空 / 算失败 → 跳过（该股该 bar 无公式信号）。
        """
        if self._tq_formula is None or not self._formula_by_strategy:
            return
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
            for code in bar.stocks:
                try:
                    if bars_by_code is not None and code in bars_by_code:
                        bars = bars_by_code[code]
                    else:
                        bars = self._dispatcher.query_quote(
                            code, period=period, count=self._formula_count
                        )
                except BridgeUnavailableError:
                    # 拉历史失败：跳过该股（不阻断 on_bar，风控信号仍可触发）
                    logger.warning("quote failed for formula inject %s %s", code, period)
                    continue
                df = self._bars_to_formula_df(bars, code)
                if df is None:
                    continue
                raw = self._tq_formula.compute_injected(
                    formula_name=formula_name, ohlcv_df=df,
                    stocks=[code], period=period,
                )
                outputs = self._extract_latest_signal(raw, code)
                if outputs:
                    self.signal_cache[(ctx.strategy_id, code, bar.bar_time)] = outputs

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
        codes = list(self._bar_poller._stock_codes)
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
                for code in self._bar_poller._stock_codes:
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
        except Exception:
            db.rollback()
            logger.exception("_poll_deals error")
        finally:
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
        self._last_backfill_time = datetime.now()  # G7：记录最近一次回填时点

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
        else:
            db.add(LiveTrade(
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
            ))

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
        return datetime.now()

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
        ctx = self._find_strategy(portfolio, live_order.strategy_id)
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
            trade_time=live_order.bar_time or datetime.now(),
            signal_type=sig_type,
        )
        if pos is None and trade.trade_type == TradeType.BUY:
            pos = Position(live_order.stock_code)
            ctx.positions[live_order.stock_code] = pos
        portfolio.account.apply_trade(trade)
        if pos is not None:
            pos.apply_trade(trade)

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
            ctx = self._find_strategy(port, tr.strategy_id)
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
