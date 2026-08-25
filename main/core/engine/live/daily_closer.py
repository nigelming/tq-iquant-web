"""DailyCloser — 日终/收盘三件套的"时点判断 + 编排"（0010 步骤 5，纯搬移）。

把原 LiveEngine 的 _maybe_daily_close / _maybe_daily_bars / _maybe_close_sweep
收敛到此处。本服务只负责"**到点 + 编排**"：
- 14:30 调 breaker.on_daily_update（daily_loss 推进/恢复，逻辑在 BreakerService）；
- 14:30 拉 1d 快照 + 1w/1mon 通达信补注入，经 on_bar 回调驱动日终 bar（行情/信号缓存
  与 bar 驱动归 MarketDataService/引擎，本服务只编排时点）；
- 15:05 收盘清扫：成交回填 + 终态同步 + 剩余未成交标 canceled（订单状态机逻辑在 OrderStateMachine）。

幂等：last_daily_date/last_daily_bar_date/last_close_sweep_date 记录当日已执行，
避免每轮 60s tick 重复触发。时间走注入 clock（默认 now_shanghai，上海时区实盘固有时点）。
非交易日（周末/节假日）三件套全部跳过，避免陈旧 bar/快照误估值或误触熔断。

本服务不反向 import LiveEngine：经 EngineContext 读 portfolios/session_id/dispatcher/db 工厂，
协作者（market_data/breaker/orders）与 on_bar、set_bridge_offline、emit 回调由构造注入。
逻辑原样搬移，仅 self.xxx → 注入对象。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Dict, Optional

from ..bar_poller import parse_bar_time, to_ohlcv
from ..event import BarEvent
from ..http_bridge_dispatcher import BridgeUnavailableError
from ...models import LiveOrder, LiveTrade
from .context import EngineContext

logger = logging.getLogger("core.engine.live.daily_closer")

# 上海 14:30 收盘后驱动（E5/E6 update_daily + C6(B/C) 1d 快照/1w/1mon 注入共用）。
_DAILY_CLOSE_TIME = (14, 30)
# 收盘清扫时点（15:05）：交易所已收盘、iQuant 自动撤单已落定后做确定性兜底。
_MARKET_CLOSE_SWEEP_TIME = (15, 5)


class DailyCloser:
    """日终/收盘时点编排：估值推进、日线驱动、收盘清扫。"""

    def __init__(
        self,
        ctx: EngineContext,
        market_data,
        breaker,
        orders,
        on_bar: Callable[..., None],
        calendar_provider: Callable[[], object],
        emit: Callable[[str, dict], None],
        set_bridge_offline: Callable[[], None],
    ) -> None:
        self._ctx = ctx
        self._market_data = market_data
        self._breaker = breaker
        self._orders = orders
        self._on_bar = on_bar
        # 日历以 provider 注入（而非对象引用）：测试在构造后会替换 engine._calendar
        # （_make_engine 后 engine._calendar = 假日历），本服务需经 provider 读到最新值。
        self._get_calendar = calendar_provider
        self._emit = emit
        self._set_bridge_offline = set_bridge_offline
        self._clock = ctx.clock

    @property
    def _calendar(self):
        return self._get_calendar()
        # E5/E6：14:30 日终已算过的日期标记（每日一次，避免每轮重复调 update_daily）
        self.last_daily_date = None
        # C6(B/C)：14:30 日终 1d 快照 bar + 1w/1mon 注入已驱动的日期标记（每日一次）
        self.last_daily_bar_date = None
        # 15:05 收盘清扫已执行的日期标记（每日一次，确定性兜底未成交单）
        self.last_close_sweep_date = None

    # ---------------- 日终估值（E5/E6）----------------
    def maybe_daily_close(self, now: Optional[datetime] = None) -> None:
        """本机时间 ≥ 14:30 且当日未算过 → 对每个组合调一次 update_daily。

        日终一次：日内盈亏 daily_pnl = 当前总市值 - prev_close（昨日收盘，update_peak 跨日刷新），
        检测 daily_loss 暂停 + 次日恢复。用上海时间（实盘固有时点，同 C6 1d 快照时点）。
        幂等：last_daily_date 记录当日已算，避免每轮循环重复触发。
        """
        if now is None:
            now = self._clock()
        if (now.hour, now.minute) < _DAILY_CLOSE_TIME:
            return
        today = now.date()
        # 非交易日（周末/节假日）不算日终：避免周末/假期 14:30 用陈旧 bar 重复估值、
        # 误触 daily_loss 或错误刷新 prev_close。桥日历离线时 fail-open（工作日照常）。
        if not self._calendar.is_trading_day(today):
            return
        if self.last_daily_date == today:
            return
        self.last_daily_date = today
        for portfolio in self._ctx.portfolios:
            # per-portfolio 日终估值 + update_daily + daily_loss 触发/恢复 + risk 事件
            # 归 BreakerService.on_daily_update（0010 步骤 4）；时点判断/幂等留本方法。
            self._breaker.on_daily_update(portfolio, today)
        logger.info(
            "daily close done (session %s, %s): %d portfolio(s) valuated",
            self._ctx.session_id, today, len(self._ctx.portfolios),
        )

    # ---------------- 14:30 日线快照 + 1w/1mon 注入（C6 B/C）----------------
    def maybe_daily_bars(self, now: Optional[datetime] = None) -> None:
        """C6(B/C)：日终（≥14:30 当日一次）1d 快照 bar + 1w/1mon 通达信注入驱动。

        **14:30 数据源定案**：
          - 1d    → **iQuant 桥** /quote?period=1d（最新 forming 1d bar 的 OHLCV 快照）
          - 1w/1mon → **通达信 TQFormula.compute**（桥端 xtdata 拉不到 1w/1mon，仅此通路）
        与启动的去重/长度规则**一致**：
          - 1d 长度按 ctx.code_period_count[(code,"1d")]（该股 1d 公式最大 formula_count，
            兜底 _period_count/全局），去重按 _sort_and_cap（stime 去重 + 截断）——同 preheat；
          - 1w/1mon 走与 start() 相同的 inject_startup_periods（count=-1 全量 + 取最新信号），
            日切 cache miss 时补注入——同启动注入。

        C6(B) 1d：拉桥 /quote?period=1d → _sort_and_cap → 构造 BarEvent(period="1d")
          → fill_signal_cache 注入（period="1d"）→ on_bar 驱动 1d 策略。
        C6(C) 1w/1mon：信号预填 signal_cache[(sid, code, daily_time)]，此处驱动命中预填信号。
        幂等：last_daily_bar_date 记录当日已触发。日切时（新 daily_time 的 1w/1mon
        cache miss）→ 通达信补注入。用本机 Asia/Shanghai 时钟（实盘固有时点，同 E5/E6）。
        """
        if now is None:
            now = self._clock()
        if (now.hour, now.minute) < _DAILY_CLOSE_TIME:
            return
        today = now.date()
        # 非交易日不驱动日线（同上：周末/假期不用陈旧快照触发 1d/1w/1mon 信号）。
        if not self._calendar.is_trading_day(today):
            return
        if self.last_daily_bar_date == today:
            return
        # 拉 1d 快照（供 1d 注入 + 构造 daily_time/stocks）。数据源 **iQuant 桥**
        # （1w/1mon 桥端 xtdata 拉不到，走通达信 inject_startup_periods，见下 C6(C)）。
        # 长度/去重规则 **同启动预热（preheat）**：
        #   长度 = 该股该周期最大 formula_count（ctx.code_period_count[(code,"1d")]，
        #     兜底周期级 _period_count/全局 _formula_count）——非周期级统一值；
        #   去重 = _sort_and_cap 按 stime 排序 + 同 stime 去重 + 截断到 count 根。
        md = self._market_data
        bars_by_code: Dict[str, list] = {}
        for code in md._bar_poller.stock_codes:
            count = self._ctx.code_period_count.get(
                (code, "1d"), md._period_count.get("1d", md._formula_count)
            )
            try:
                bars = self._ctx.dispatcher.query_quote(
                    code, period="1d", count=count
                )
            except BridgeUnavailableError:
                self._set_bridge_offline()
                logger.warning("bridge offline on daily bars (session %s)", self._ctx.session_id)
                return
            if bars:
                bars_by_code[code] = md._sort_and_cap(bars, count)
        if not bars_by_code:
            return
        # daily_time = 任一 code 最新 1d bar 的 stime（交易日 00:00）；解析失败用今日零点兜底
        daily_time = parse_bar_time(next(iter(bars_by_code.values()))[-1])
        if daily_time is None:
            daily_time = datetime.combine(today, datetime.min.time())
        self.last_daily_bar_date = today
        # 日切检测：新 daily_time 的 1w/1mon cache miss → 通达信补注入
        reinjected = md.startup_periods_missing(daily_time)
        if reinjected:
            md.inject_startup_periods(daily_time)
        # 1d 快照即最终值（14:30 后），每 code 取最新 forming 1d bar 的 OHLCV
        stocks = {code: to_ohlcv(bars[-1]) for code, bars in bars_by_code.items()}
        # C4(#28)：跨三周期共享去重缓存（key 含 period，1d/1w/1mon 互不干扰）
        df_cache: Dict = {}
        raw_cache: Dict = {}
        for period in ("1d", "1w", "1mon"):
            bar_event = BarEvent(stocks=stocks, bar_time=daily_time, period=period)
            for portfolio in self._ctx.portfolios:
                try:
                    self._on_bar(
                        portfolio, bar_event, bars_by_code=bars_by_code,
                        df_cache=df_cache, raw_cache=raw_cache,
                    )
                except BridgeUnavailableError as e:
                    self._set_bridge_offline()
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
            self._ctx.session_id, daily_time, "1d", len(bars_by_code),
            reinjected,
        )

    # ---------------- 收盘清扫（15:05 确定性兜底）----------------
    def maybe_close_sweep(self, now: Optional[datetime] = None) -> None:
        """交易日 15:05 后对当日仍未完结单做一次确定性兜底（幂等）。

        实时轮询（orders.poll_deals，5s）是「尽力而为」：桥抖动/被饿死/ORDER 实时表移除单
        都可能让单残留 submitted/partial。收盘后状态已全部收敛，做一次权威清扫：
          1) /orders + /deals 各查一次，补 order_ref 定位 + 按 order_ref/remark 回填成交；
          2) sync_terminal_order_status 处理 53部撤/54全撤/57废单（含 partial 先 apply 再 cancel）；
          3) A 股规则：收盘仍未成交（含 48-52 在途/55 部成/实时表缺席）的单一律标 canceled
             ——收盘后不会再有成交，未成交部分被交易所/券商自动撤。partial 已回填的部分成交
             先 apply 落持仓再 cancel（与 53 部撤同处理，不丢成交）。
        与实时轮询的关系：清扫是「权威收口」，实时是「近实时反馈」；清扫幂等可重复执行。
        非交易日/桥离线：跳过（下轮或下个交易日重试，不误标）。
        """
        if now is None:
            now = self._clock()
        if (now.hour, now.minute) < _MARKET_CLOSE_SWEEP_TIME:
            return
        today = now.date()
        if not self._calendar.is_trading_day(today):
            return
        if self.last_close_sweep_date == today:
            return
        db = self._ctx.db_session_factory()
        try:
            pending = (
                db.query(LiveOrder)
                .filter(
                    LiveOrder.live_session_id == self._ctx.session_id,
                    LiveOrder.status.in_(["submitted", "partial"]),
                )
                .all()
            )
            if not pending:
                self.last_close_sweep_date = today
                logger.info(
                    "close sweep: no pending orders, nothing to do (session %s, %s)",
                    self._ctx.session_id, today,
                )
                return
            logger.info(
                "close sweep start (session %s, %s): %d pending order(s)",
                self._ctx.session_id, today, len(pending),
            )
            # 1) 一次 /orders + /deals：补 ref + 回填成交（复用实时轮询同套逻辑）。
            try:
                orders = self._ctx.dispatcher.query_orders()
            except BridgeUnavailableError:
                logger.warning("close sweep: bridge offline (/orders), skip")
                return
            claimed_refs = set(
                r[0] for r in db.query(LiveOrder.order_ref).filter(
                    LiveOrder.live_session_id == self._ctx.session_id,
                    LiveOrder.order_ref.isnot(None),
                ).all()
            )
            for lo in pending:
                if lo.order_ref is None:
                    self._orders.try_match_order_ref(lo, claimed_refs, orders=orders)
                    if lo.order_ref is not None:
                        claimed_refs.add(lo.order_ref)
            db.commit()

            try:
                deals = self._ctx.dispatcher.query_deals()
            except BridgeUnavailableError:
                deals = None
            if deals is not None:
                for lo in pending:
                    if lo.status not in ("submitted", "partial"):
                        continue
                    if lo.order_ref is not None:
                        matched = [d for d in deals if d.get("order_ref") == lo.order_ref]
                        if matched:
                            self._orders.backfill_order(db, lo, matched)
                            continue
                    if lo.bridge_order_id:
                        expected_remark = lo.bridge_order_id[:20]
                        matched = [d for d in deals if d.get("remark") == expected_remark]
                        if matched:
                            self._orders.backfill_order(db, lo, matched)
                db.commit()

            # 2) 终态同步：53/54 → canceled（partial 先 apply）、57 → rejected。
            self._orders.sync_terminal_order_status(db, pending, orders, deals)
            db.commit()

            # 3) A 股收盘兜底：仍在 submitted/partial 的单 = 收盘未成交 → canceled。
            #    实时表缺席（已撤单被移除）或状态仍在途（48-52/55/255）都落此：收盘后
            #    不会再成交，未成交即撤。已有部分成交（filled_quantity>0）先 apply 再 cancel。
            #
            #    资金安全：若 /orders 报 traded_volume > filled_quantity，说明 /deals 成交回报
            #    滞后（成交价/量/金额尚未回填齐），此时绝不能 cancel——会丢真实成交。延后本轮
            #    （不置 last_close_sweep_date），下轮 60s tick 等 /deals 追上再清扫，与
            #    sync_terminal_order_status 同一守卫。仅对 /orders 里**确实查到**的单可比
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
                        self._orders.apply_filled_trade(
                            lo, trade.price, trade.quantity, trade.amount, trade.commission
                        )
                lo.status = "canceled"
                if not lo.error_message:
                    lo.error_message = "close sweep: unfilled remainder canceled after market close"
                self._orders.pending_orders.pop(lo.id, None)
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
            self._orders.sync_pending_orders(db)
            if deferred:
                # 有成交回报滞后的单：本轮不标记完成，下轮 60s tick 重试，等 /deals 回填齐。
                logger.warning(
                    "close sweep: %d order(s) deferred for /deals backfill, will retry "
                    "(session %s, %s)", deferred, self._ctx.session_id, today,
                )
                return
            self.last_close_sweep_date = today
            logger.info("close sweep done (session %s, %s)", self._ctx.session_id, today)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("close sweep error (session %s)", self._ctx.session_id)
        finally:
            db.close()
