from typing import Dict, List, Optional
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
        """逐 bar 回测主链路。

        klines: {stock_code: {period: pl.DataFrame}}，DataFrame 含 datetime + Open/High/Low/Close/Volume 列。
        成交时机：上一 bar 触发的订单在下一 bar open 成交（pending_orders 队列），
        成交价取 price_index 中该 bar 的 Open（open_prices 死参数已移除，#12）。
        """
        klines = klines or {}
        signal_cache = signal_cache if signal_cache is not None else {}

        # 决策闸门采集（调参可观测性）：注入组合与执行引擎，run 结束随结果返回
        recorder = DecisionRecorder()
        portfolio.set_recorder(recorder)

        times = self._build_timeline(klines)
        price_index = self._build_price_index(klines)
        pending_orders: List[OrderEvent] = []
        trades: List[TradeEvent] = []
        snapshots: List[dict] = []
        # #2：已记过 no_market_data 决策事件的订单 id（仅首次延后记 1 条，长停牌不刷屏）
        no_price_recorded: set = set()
        # 策略层快照：strategy_id → 快照序列。共享现金按 capital_ratio 归一化分摊。
        strategy_snapshots: Dict[int, List[dict]] = {
            ctx.strategy_id: [] for ctx in portfolio.strategies
        }

        for i, t in enumerate(times):
            # 1. 成交上一 bar 的 pending_orders（用 t 的 open 价）
            deferred: List[OrderEvent] = []
            if pending_orders:
                bar_open_prices = self._bar_open_prices(price_index, t)
                dispatcher = SimulatedDispatcher(bar_open_prices, **portfolio.cost_params)
                engine = ExecutionEngine(dispatcher, SimulatedT1Checker(), recorder=recorder)
                # 未来函数闸门：日线级信号（signal_bar_date 非 None）须在
                # t.date() > signal_bar_date 的下一交易日成交，防止当日 00:00
                # 日线 bar 的信号在当日 09:30 分钟 bar 成交。未满足的订单延后。
                for order in pending_orders:
                    if order.signal_bar_date is not None and t.date() <= order.signal_bar_date:
                        deferred.append(order)
                        continue
                    # #2 静默丢弃修复：停牌/无行情（本 bar 该票无有效 open 价）→
                    # 延后至下一有价 bar 重试，不静默丢弃（此前直接丢弃导致
                    # 停牌期间止损/止盈单永久丢失）。仅首次延后记决策事件。
                    if not bar_open_prices.get(order.stock_code):
                        if id(order) not in no_price_recorded:
                            no_price_recorded.add(id(order))
                            recorder.record(
                                gate="no_market_data", layer="execution", action="defer",
                                portfolio_id=order.portfolio_id or 0,
                                strategy_id=order.strategy_id,
                                stock_code=order.stock_code,
                                bar_time=t,
                                message="停牌/无行情：本 bar 无开盘价，订单延后至下一有价 bar 重试",
                            )
                        deferred.append(order)
                        continue
                    # 成交日 = 当前 bar 时间 t（T+1 据此判断）
                    order.bar_time = t
                    ctx = portfolio.find_strategy(order.strategy_id)
                    if ctx is None:
                        continue
                    # BUY 首次建仓：确保 Position 存在于 ctx.positions
                    pos = ctx.positions.get(order.stock_code)
                    if pos is None and order.trade_type.value == "BUY":
                        pos = Position(order.stock_code)
                        ctx.positions[order.stock_code] = pos
                    trade = engine.execute(order, portfolio.account, pos)
                    if trade is not None:
                        trades.append(trade)

            # 2. 构造 BarEvent（从预建索引 O(1) 查表）
            bar = self._build_bar(price_index, t)

            # 3. portfolio.on_bar → 新订单入 pending（下一 bar 成交）
            #    deferred（未到成交日的日线信号）需携带至后续 bar 合并
            new_orders = portfolio.on_bar(bar, signal_cache=signal_cache)
            pending_orders = deferred + new_orders

            # 4. 日终快照（日线：每根 bar 即一日）
            total_value = portfolio.total_value(bar)
            snap = portfolio.snapshot(t.date(), total_value, bar)
            # 填基准值：benchmark_data 是 {date: Decimal}，按当前 bar 日期取值；
            # 缺失（基准停牌/非交易日/区间外）直接为 None（不做 carry-forward），
            # Evaluator 喂基准序列时会过滤 None 值，全无则 benchmark_return 退化为 0。
            snap["benchmark_value"] = benchmark_data.get(t.date()) if benchmark_data else None
            snapshots.append(snap)

            # 4b. 策略层快照：共享现金按 capital_ratio 归一化分摊，保证可加性
            #     Σ(策略总净值) == 组合总净值。供策略层独立净值曲线与评估。
            #     注入与组合同日的 benchmark_value，使策略层相对指标（FR-1）可算。
            for s_snap in self._strategy_snapshots(portfolio, bar, t.date()):
                s_snap["benchmark_value"] = snap["benchmark_value"]
                strategy_snapshots[s_snap["target_id"]].append(s_snap)

            # 5. 熔断检测：日终更新峰值/日内盈亏，按 §88 推进熔断次日恢复时序
            portfolio.risk_manager.update(
                total_value, t.date(), portfolio.account.initial_capital
            )

        # 6. 补算最后一日 daily_loss（#8 对齐实盘 14:30 时点：循环内 update 只在
        #    跨日时补算上一日，最后一日无后续跨日触发，需显式收尾）
        portfolio.risk_manager.finalize_last_day(portfolio.account.initial_capital)

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

    def _build_price_index(self, klines: dict) -> Dict[datetime, Dict[str, dict]]:
        """预建 {datetime: {stock_code: {open,high,low,close,volume}}} 索引。

        一次 O(total_rows) 扫描替代逐 bar 的 O(n²) polars df.filter()，将每 bar
        的 72 次 Python↔Rust FFI 查表降为 O(1) 字典查找。
        多周期重叠时间点时，后周期覆盖前周期（与旧 _build_bar 行为兼容）。
        """
        index: Dict[datetime, Dict[str, dict]] = {}
        for stock_code, periods in klines.items():
            for period, df in periods.items():
                if "datetime" not in df.columns:
                    continue
                times = df["datetime"].to_list()
                if "Open" not in df.columns:
                    continue
                opens = df["Open"].to_list()
                highs = df["High"].to_list()
                lows = df["Low"].to_list()
                closes = df["Close"].to_list()
                volumes = df["Volume"].to_list()
                for i, t in enumerate(times):
                    row = {
                        "open": opens[i],
                        "high": highs[i],
                        "low": lows[i],
                        "close": closes[i],
                        "volume": volumes[i],
                    }
                    index.setdefault(t, {})[stock_code] = row
        return index

    def _bar_open_prices(self, price_index: dict, t: datetime) -> Dict[str, Decimal]:
        """O(1) 查表取时间 t 所有股票的 open 价。"""
        row = price_index.get(t, {})
        return {code: data["open"] for code, data in row.items()}

    def _build_bar(self, price_index: dict, t: datetime) -> BarEvent:
        """O(1) 查表取时间 t 的 OHLCV 构造 BarEvent。"""
        return BarEvent(stocks=price_index.get(t, {}), bar_time=t)

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
