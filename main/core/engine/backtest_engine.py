from typing import Dict, List
from datetime import datetime, date
from decimal import Decimal

from .portfolio import Portfolio
from .position import Position
from .execution_engine import ExecutionEngine, SimulatedDispatcher, SimulatedT1Checker
from .evaluator import Evaluator
from .event import BarEvent, OrderEvent, TradeEvent
from .decision import DecisionRecorder


class BacktestEngine:
    def run(
        self,
        portfolio: Portfolio,
        klines: dict = None,
        signal_cache: dict = None,
        benchmark_data: object = None,
    ) -> dict:
        """逐刻回测主链路（按周期分层驱动）。

        klines: {stock_code: {period: pl.DataFrame}}，DataFrame 含 datetime + Open/High/Low/Close/Volume 列。
        成交时机：订单只在其所属周期的下一根同周期 bar open 成交（pending_by_period 队列）。
        """
        klines = klines or {}
        signal_cache = signal_cache if signal_cache is not None else {}

        # 决策闸门采集（调参可观测性）：注入组合与执行引擎，run 结束随结果返回
        recorder = DecisionRecorder()
        portfolio.set_recorder(recorder)

        times = self._build_timeline(klines)
        period_index = self._build_price_index_by_period(klines)   # {period: {t: {code: row}}}
        periods_at: Dict[datetime, List[str]] = {}
        for period, by_t in period_index.items():
            for t in by_t:
                periods_at.setdefault(t, []).append(period)
        for t in periods_at:
            periods_at[t].sort()

        pending_by_period: Dict[str, List[OrderEvent]] = {p: [] for p in period_index}
        trades: List[TradeEvent] = []
        snapshots: List[dict] = []
        # 策略层快照：strategy_id → 快照序列。共享现金按 capital_ratio 归一化分摊。
        strategy_snapshots: Dict[int, List[dict]] = {
            ctx.strategy_id: [] for ctx in portfolio.strategies
        }
        initial = portfolio.account.initial_capital

        for i, t in enumerate(times):
            # 1) 成交步：逐周期、对"本周期本刻有 bar"的挂单成交
            for period in periods_at.get(t, ()):
                rows = period_index[period].get(t, {})
                if not rows:
                    continue
                opens = {c: d["open"] for c, d in rows.items()}
                dispatcher = SimulatedDispatcher(opens, **portfolio.cost_params)
                executor = ExecutionEngine(dispatcher, SimulatedT1Checker(), recorder=recorder)
                remaining: List[OrderEvent] = []
                for order in pending_by_period[period]:
                    if order.stock_code not in opens:
                        remaining.append(order)      # 无 bar 保留（#2 TTL 在 Task 4）
                        continue
                    order.bar_time = t
                    ctx = portfolio.find_strategy(order.strategy_id)
                    if ctx is None:
                        continue
                    pos = ctx.positions.get(order.stock_code)
                    if pos is None and order.trade_type.value == "BUY":
                        pos = Position(order.stock_code)
                        ctx.positions[order.stock_code] = pos
                    trade = executor.execute(order, portfolio.account, pos)
                    if trade is not None:
                        trades.append(trade)
                pending_by_period[period] = remaining

            # 2) 信号步：逐周期构造 bar 驱动对应周期策略
            for period in periods_at.get(t, ()):
                rows = period_index[period].get(t, {})
                if not rows:
                    continue
                bar = BarEvent(stocks=rows, bar_time=t, period=period)
                new_orders = portfolio.on_bar(bar, signal_cache=signal_cache, period=period)
                for o in new_orders:
                    o.period = period
                pending_by_period[period].extend(new_orders)

            # 3) 估值 bar（合并本刻所有周期行，按周期升序遍历，粗周期覆盖细）
            value_bar = BarEvent(stocks=self._merge_rows(period_index, periods_at.get(t, ()), t), bar_time=t)
            total_value = portfolio.total_value(value_bar)

            # 4) 每 bar 更新峰值/回撤（intraday peak，#8）
            portfolio.risk_manager.update_peak(total_value, t.date())

            # 5) 日末（本日最后一根 bar）：快照 + 更新日内亏损
            is_day_end = (i == len(times) - 1) or (times[i + 1].date() != t.date())
            if is_day_end:
                snap = portfolio.snapshot(t.date(), total_value, value_bar)
                # 基准值按 bar 日期取；缺失即 None（不沿用上一已知值——原注释与实现不符，#13）
                snap["benchmark_value"] = benchmark_data.get(t.date()) if benchmark_data else None
                snapshots.append(snap)
                for s_snap in self._strategy_snapshots(portfolio, value_bar, t.date()):
                    s_snap["benchmark_value"] = snap["benchmark_value"]
                    strategy_snapshots[s_snap["target_id"]].append(s_snap)
                portfolio.risk_manager.update_daily(total_value, t.date(), initial)

        # 基准序列喂 Evaluator
        # 形如 [{"value": Decimal}]。无任何基准值 → 传 None，benchmark_return 退化为 0。
        bench_series = (
            [{"value": s["benchmark_value"]} for s in snapshots if s.get("benchmark_value") is not None]
            or None
        )
        evaluations = Evaluator().evaluate(snapshots, benchmark_data=bench_series, trades=trades)
        # 策略层评估：复用 Evaluator，喂入该策略快照 + 该策略 trades（按 strategy_id 过滤）。
        # 传入 bench_series 使策略层也能计算基准相对指标（beta/alpha/IR/捕获率等）。
        strategy_evaluations: Dict[int, dict] = {}
        for ctx in portfolio.strategies:
            s_snaps = strategy_snapshots.get(ctx.strategy_id, [])
            if len(s_snaps) < 2:
                continue
            s_trades = [t for t in trades if t.strategy_id == ctx.strategy_id]
            strategy_evaluations[ctx.strategy_id] = Evaluator().evaluate(
                s_snaps, benchmark_data=bench_series, trades=s_trades
            )
        return {
            "trades": trades,
            "snapshots": snapshots,
            "evaluations": evaluations,
            "strategy_snapshots": strategy_snapshots,
            "strategy_evaluations": strategy_evaluations,
            # 决策闸门事件（止损/止盈/熔断/资金不足/超仓…逐触发一条），供落库与调参统计
            "decisions": [ev.to_dict() for ev in recorder.drain()],
        }

    def _build_timeline(self, klines: dict) -> List[datetime]:
        """合并所有股票所有周期的时间轴，排序去重。"""
        times: List[datetime] = []
        for stock_code, periods in klines.items():
            for period, df in periods.items():
                if "datetime" not in df.columns:
                    continue
                times.extend(df["datetime"].to_list())
        # 去重排序
        seen = set()
        unique = []
        for t in sorted(times):
            if t not in seen:
                seen.add(t)
                unique.append(t)
        return unique

    def _build_price_index_by_period(self, klines: dict) -> Dict[str, Dict[datetime, Dict[str, dict]]]:
        """{period: {datetime: {stock_code: {open,high,low,close,volume}}}}（#1b 同刻多周期分层）。"""
        index: Dict[str, Dict[datetime, Dict[str, dict]]] = {}
        for stock_code, periods in klines.items():
            for period, df in periods.items():
                if "datetime" not in df.columns or "Open" not in df.columns:
                    continue
                times = df["datetime"].to_list()
                opens = df["Open"].to_list(); highs = df["High"].to_list()
                lows = df["Low"].to_list(); closes = df["Close"].to_list()
                vols = df["Volume"].to_list()
                by_t = index.setdefault(period, {})
                for k, tb in enumerate(times):
                    by_t.setdefault(tb, {})[stock_code] = {
                        "open": opens[k], "high": highs[k], "low": lows[k],
                        "close": closes[k], "volume": vols[k],
                    }
        return index

    def _merge_rows(self, period_index: dict, periods: List[str], t: datetime) -> Dict[str, dict]:
        """合并本刻所有周期行。period 列表已排序，后（粗）周期覆盖先（细）周期。"""
        rows: Dict[str, dict] = {}
        for period in periods:
            rows.update(period_index[period].get(t, {}))
        return rows

    def _strategy_snapshots(
        self, portfolio: Portfolio, bar: BarEvent, snap_date: date
    ) -> List[dict]:
        """各策略日终快照。共享现金按 capital_ratio 归一化分摊，保证可加性：
        Σ(策略总净值) == 组合总净值。
        策略总净值 = 策略持股市值 + 分摊现金；分摊现金 = 组合现金 × (该 ratio / Σratio)。
        Σratio=0（极端情况）退化为均分，避免除零。
        """
        sum_ratio = sum((ctx.capital_ratio for ctx in portfolio.strategies), Decimal("0"))
        cash = portfolio.account.cash
        # 与组合层 total_value 同口径：刷新价格快照后按最近已知价估每策略持仓市值，
        # 缺席（停牌/缺 bar）沿用昨收而非按 0，保证 Σ策略市值 == 组合市值。
        portfolio._refresh_price_snapshot(bar)
        snaps: List[dict] = []
        for ctx in portfolio.strategies:
            market_value = portfolio.holdings_value(ctx)
            if sum_ratio > 0:
                allocated_cash = cash * (ctx.capital_ratio / sum_ratio)
            else:
                n = max(len(portfolio.strategies), 1)
                allocated_cash = cash / Decimal(n)
            total_value = market_value + allocated_cash
            snaps.append({
                "target_type": "strategy",
                "target_id": ctx.strategy_id,
                "snap_date": snap_date,
                "total_value": total_value,
                "cash": allocated_cash,
                "market_value": market_value,
                # 策略层持仓明细（只含本策略，供 strategy 维度 positions_json）
                "positions": portfolio.positions_detail(snap_date, ctx=ctx),
            })
        return snaps
