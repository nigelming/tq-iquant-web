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
from .bar_poller import BarPoller
from core.models import LiveOrder, LiveTrade
from core.tq.formula import TQFormula
from tq_iquant_shared.constants import SignalType, TradeType

logger = logging.getLogger(__name__)

# TQ 公式输出中需跳过的非变量键（同 backtest._FORMULA_META_KEYS）
_FORMULA_META_KEYS = ("Date", "ErrorId", "Error", "Time")


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
        poll_interval: float = 15.0,
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
        # 切片5（I4）：Core 重启后从 DB 挂回的未完结 LiveOrder（submitted/partial），
        # 主循环 _poll_deals 据此轮询 /deals 回填。key=LiveOrder.id。
        self._pending_orders: Dict[int, "LiveOrder"] = {}

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        """起 asyncio 循环任务，绑定 BarPoller.on_bar 回调。"""
        if self._running:
            return
        self._running = True
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
        """主循环：心跳 → 拉 bar → sleep。桥离线则暂停下单、标状态，不抛异常。"""
        while self._running:
            try:
                if not self._dispatcher.heartbeat():
                    self._bridge_online = False
                    logger.warning("bridge offline, pause trading (session %s)", self.session_id)
                    await asyncio.sleep(self._poll_interval)
                    continue
                self._bridge_online = True
                # poll() 内部对每根完成的 bar 触发 self._on_bar 回调
                self._bar_poller.poll()
                # E5/E6：14:30 日终一次 update_daily（日内亏损/熔断次日恢复推进）
                self._maybe_daily_close()
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

    # ---------------- bar 驱动 ----------------
    def _on_bar(self, bar: BarEvent) -> None:
        """BarPoller 回调：对每个组合驱动 _handle_bar。"""
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

    def _handle_bar(self, portfolio: Portfolio, bar: BarEvent) -> None:
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
        self._fill_signal_cache(portfolio, bar)
        orders = portfolio.on_bar(bar, signal_cache=self.signal_cache)
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
                # ① 先写 submitted + commit（I4 命门窗口闭合）
                live_order = self._persist_order_submitted(db, order)
                db.commit()
                try:
                    # ② 再发 passorder（apply=False：submitted 阶段不更新账户持仓）
                    trade = self._engine.execute(order, portfolio.account, pos, apply=False)
                except BridgeUnavailableError:
                    # 桥离线：标 rejected + 置离线暂停（_on_bar 上层心跳循环据此暂停下单）
                    live_order.status = "rejected"
                    live_order.error_message = "bridge unavailable"
                    self._bridge_online = False
                    db.commit()
                    continue
                if trade is None:
                    # 审批不过 / 桥业务拒绝 → rejected（不成交，不 apply）
                    live_order.status = "rejected"
                    live_order.error_message = "approval failed or bridge rejected"
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
    def _fill_signal_cache(self, portfolio: Portfolio, bar: BarEvent) -> None:
        """实盘逐 bar 算公式信号填 signal_cache。预填模式（不改 Portfolio）。

        对每个策略 × bar.stocks 每只股票：
          bridge query_quote(code, period, count=N) 拉历史+实时 bar
          → _bars_to_formula_df 转 OHLCV DataFrame
          → TQFormula.compute_injected 内存注入算公式
          → _extract_latest_signal 取最后一条（当前 bar 信号）
          → 填 signal_cache[(strategy_id, code, bar.bar_time)]
        无 tq_formula / 策略无公式映射 / 拉取为空 / 算失败 → 跳过（该股该 bar 无公式信号）。
        """
        if self._tq_formula is None or not self._formula_by_strategy:
            return
        for ctx in portfolio.strategies:
            formula_name = self._formula_by_strategy.get(ctx.strategy_id)
            if not formula_name:
                continue
            period = ctx.period
            for code in bar.stocks:
                try:
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

        桥 bar 字段：index(yyyymmddHHMMSS)/open/high/low/close/volume/amount（小写）。
        输出：每字段单列 DataFrame（列=[code]，行=DatetimeIndex，从 index 解析）。
        空 bars / 无有效时间 → None（调用方跳过）。
        """
        if not bars:
            return None
        import pandas as pd

        times, o, h, l, c, v, a = [], [], [], [], [], [], []
        for b in bars:
            idx = b.get("index")
            if not idx:
                continue
            s = str(idx).strip()
            try:
                if len(s) >= 14 and s[:14].isdigit():
                    t = datetime.strptime(s[:14], "%Y%m%d%H%M%S")
                elif len(s) >= 8 and s[:8].isdigit():
                    t = datetime.strptime(s[:8], "%Y%m%d")
                else:
                    continue
            except ValueError:
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
                return
            # 1. 未回填 order_ref 的单：尝试定位
            for lo in pending:
                if lo.order_ref is None:
                    self._try_match_order_ref(lo)
            db.commit()
            # 2. 有 order_ref 的单：查 /deals 回填
            need_ref = [lo for lo in pending if lo.order_ref is not None]
            if not need_ref:
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
        except Exception:
            db.rollback()
            logger.exception("_poll_deals error")
        finally:
            db.close()

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
    def dispatcher(self) -> HttpBridgeDispatcher:
        """暴露 dispatcher 供桥状态查询等只读访问。"""
        return self._dispatcher
