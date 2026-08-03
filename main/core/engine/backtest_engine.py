from typing import Callable, Dict, List, Optional
from datetime import datetime, date
from decimal import Decimal

import polars as pl

from .portfolio import Portfolio
from .strategy_context import StrategyContext
from .position import Position
from .execution_engine import ExecutionEngine, SimulatedDispatcher, SimulatedT1Checker
from .evaluator import Evaluator
from .event import BarEvent, OrderEvent, TradeEvent


class BacktestEngine:
    def run(
        self,
        portfolio: Portfolio,
        klines: dict = None,
        signal_cache: dict = None,
        open_prices: Optional[Dict[str, Dict[datetime, Decimal]]] = None,
        benchmark_data: object = None,
        progress_callback: Callable = None,
    ) -> dict:
        """逐 bar 回测主链路。

        klines: {stock_code: {period: pl.DataFrame}}，DataFrame 含 datetime + Open/High/Low/Close/Volume 列。
        open_prices: {stock_code: {bar_time: open_price}}，待执行订单在下一 bar 的 open 价成交。
        成交时机：上一 bar 触发的订单在下一 bar open 成交（pending_orders 队列）。
        """
        klines = klines or {}
        signal_cache = signal_cache if signal_cache is not None else {}
        open_prices = open_prices or {}

        times = self._build_timeline(klines)
        pending_orders: List[OrderEvent] = []
        trades: List[TradeEvent] = []
        snapshots: List[dict] = []
        # 策略层快照：strategy_id → 快照序列。共享现金按 capital_ratio 归一化分摊。
        strategy_snapshots: Dict[int, List[dict]] = {
            ctx.strategy_id: [] for ctx in portfolio.strategies
        }

        for i, t in enumerate(times):
            # 1. 成交上一 bar 的 pending_orders（用 t 的 open 价）
            if pending_orders:
                bar_open_prices = self._bar_open_prices(klines, t)
                dispatcher = SimulatedDispatcher(bar_open_prices, **portfolio.cost_params)
                engine = ExecutionEngine(dispatcher, SimulatedT1Checker())
                for order in pending_orders:
                    # 成交日 = 当前 bar 时间 t（T+1 据此判断）
                    order.bar_time = t
                    ctx = self._find_strategy(portfolio, order.strategy_id)
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
                pending_orders = []

            # 2. 构造 BarEvent（从 polars 取 t 行 OHLCV）
            bar = self._build_bar(klines, t)

            # 3. portfolio.on_bar → 新订单入 pending（下一 bar 成交）
            pending_orders = portfolio.on_bar(bar, signal_cache=signal_cache)

            # 4. 日终快照（日线：每根 bar 即一日）
            total_value = self._total_value(portfolio, bar)
            snap = portfolio.snapshot(t.date(), total_value, bar)
            # 填基准值：benchmark_data 是 {date: Decimal}，按当前 bar 日期取值；
            # 缺失则沿用上一已知值（基准停牌/非交易日），全无则 None。
            snap["benchmark_value"] = benchmark_data.get(t.date()) if benchmark_data else None
            snapshots.append(snap)

            # 4b. 策略层快照：共享现金按 capital_ratio 归一化分摊，保证可加性
            #     Σ(策略总净值) == 组合总净值。供策略层独立净值曲线与评估。
            for s_snap in self._strategy_snapshots(portfolio, bar, t.date()):
                strategy_snapshots[s_snap["target_id"]].append(s_snap)

            # 5. 熔断检测：日终更新峰值/日内盈亏，按 §88 推进熔断次日恢复时序
            portfolio.risk_manager.update(
                total_value, t.date(), portfolio.account.initial_capital
            )

            if progress_callback:
                progress_callback(i + 1)

        # 基准序列喂 Evaluator：从快照 benchmark_value 抽取（按日期顺序），
        # 形如 [{"value": Decimal}]。无任何基准值 → 传 None，benchmark_return 退化为 0。
        bench_series = (
            [{"value": s["benchmark_value"]} for s in snapshots if s.get("benchmark_value") is not None]
            or None
        )
        evaluations = Evaluator().evaluate(snapshots, benchmark_data=bench_series, trades=trades)
        # 策略层评估：复用 Evaluator，喂入该策略快照 + 该策略 trades（按 strategy_id 过滤）。
        strategy_evaluations: Dict[int, dict] = {}
        for ctx in portfolio.strategies:
            s_snaps = strategy_snapshots.get(ctx.strategy_id, [])
            if len(s_snaps) < 2:
                continue
            s_trades = [t for t in trades if t.strategy_id == ctx.strategy_id]
            strategy_evaluations[ctx.strategy_id] = Evaluator().evaluate(
                s_snaps, benchmark_data=None, trades=s_trades
            )
        return {
            "trades": trades,
            "snapshots": snapshots,
            "evaluations": evaluations,
            "strategy_snapshots": strategy_snapshots,
            "strategy_evaluations": strategy_evaluations,
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

    def _bar_open_prices(self, klines: dict, t: datetime) -> Dict[str, Decimal]:
        """取时间 t 所有股票的 open 价，供 dispatcher 成交。"""
        prices: Dict[str, Decimal] = {}
        for stock_code, periods in klines.items():
            for period, df in periods.items():
                if "datetime" not in df.columns or "Open" not in df.columns:
                    continue
                row = df.filter(pl.col("datetime") == t)
                if row.height > 0:
                    prices[stock_code] = row["Open"][0]
        return prices

    def _build_bar(self, klines: dict, t: datetime) -> BarEvent:
        """从 polars 取 t 行构造 BarEvent。"""
        stocks: Dict[str, Dict[str, object]] = {}
        for stock_code, periods in klines.items():
            for period, df in periods.items():
                if "datetime" not in df.columns:
                    continue
                row = df.filter(pl.col("datetime") == t)
                if row.height == 0:
                    continue
                stocks[stock_code] = {
                    "open": row["Open"][0],
                    "high": row["High"][0],
                    "low": row["Low"][0],
                    "close": row["Close"][0],
                    "volume": row["Volume"][0],
                }
                break  # 单周期取第一份
        return BarEvent(stocks=stocks, bar_time=t)

    def _find_strategy(
        self, portfolio: Portfolio, strategy_id: int
    ) -> Optional[StrategyContext]:
        for ctx in portfolio.strategies:
            if ctx.strategy_id == strategy_id:
                return ctx
        return None

    def _total_value(self, portfolio: Portfolio, bar: BarEvent) -> Decimal:
        """组合总市值 = 现金 + 所有策略持仓按当前 close 的市值。"""
        total = portfolio.account.cash
        for ctx in portfolio.strategies:
            for stock_code, pos in ctx.positions.items():
                if pos.quantity == 0 or stock_code not in bar.stocks:
                    continue
                close = bar.stocks[stock_code]["close"]
                total += close * pos.quantity
        return total

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
        snaps: List[dict] = []
        for ctx in portfolio.strategies:
            market_value = Decimal("0")
            for stock_code, pos in ctx.positions.items():
                if pos.quantity == 0 or stock_code not in bar.stocks:
                    continue
                market_value += bar.stocks[stock_code]["close"] * pos.quantity
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
            })
        return snaps
