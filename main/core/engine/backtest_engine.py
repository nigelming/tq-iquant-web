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

        for i, t in enumerate(times):
            # 1. 成交上一 bar 的 pending_orders（用 t 的 open 价）
            if pending_orders:
                bar_open_prices = self._bar_open_prices(klines, t)
                dispatcher = SimulatedDispatcher(bar_open_prices)
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
            snapshots.append(portfolio.snapshot(t.date(), total_value, bar))

            if progress_callback:
                progress_callback(i + 1)

        evaluations = Evaluator().evaluate(snapshots, benchmark_data=benchmark_data)
        return {"trades": trades, "snapshots": snapshots, "evaluations": evaluations}

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
