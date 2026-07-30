from typing import List, Dict, Optional
from decimal import Decimal
from datetime import date

from .account import Account
from .strategy_context import StrategyContext
from .position import Position
from .risk_manager import PortfolioRiskManager
from .event import BarEvent, SignalEvent, OrderEvent
from tq_iquant_shared.constants import SignalType, TradeType

# 公式信号类型优先级（同策略内）：CLOSE > REDUCE > ADD > OPEN
_FORMULA_PRIORITY = {
    SignalType.CLOSE: 0,
    SignalType.REDUCE: 1,
    SignalType.ADD: 2,
    SignalType.OPEN: 3,
}
# 风控信号优先级（高于所有公式信号）
_RISK_PRIORITY = {
    SignalType.STOP_LOSS: -1,
    SignalType.TAKE_PROFIT: -1,
    SignalType.TRAILING_STOP: -1,
}
# 全平类信号：生成订单后标记该股票清仓，抑制同 bar 后续信号
_FULL_CLOSE_TYPES = {
    SignalType.CLOSE, SignalType.STOP_LOSS,
    SignalType.TAKE_PROFIT, SignalType.TRAILING_STOP,
}


def _signal_priority(signal_type: SignalType) -> int:
    if signal_type in _RISK_PRIORITY:
        return _RISK_PRIORITY[signal_type]
    return _FORMULA_PRIORITY.get(signal_type, 99)


class Portfolio:
    def __init__(
        self,
        portfolio_id: int,
        initial_capital: Decimal,
        risk_manager: PortfolioRiskManager,
    ):
        self.portfolio_id = portfolio_id
        self.account = Account(initial_capital)
        self.risk_manager = risk_manager
        self.strategies: List[StrategyContext] = []
        self.benchmark_value: Optional[Decimal] = None

    def on_bar(
        self,
        bar: BarEvent,
        signal_cache: Optional[Dict] = None,
    ) -> List[OrderEvent]:
        """处理一根 bar：取信号 + 风控检查 + 优先级排序 → 返回待执行订单列表。

        订单在下一个 bar 的 open 成交（由 BacktestEngine 调度）。
        信号优先级：风控（止损/止盈/移动止损）> 公式；公式内 CLOSE>REDUCE>ADD>OPEN。
        风控清仓后公式信号不再执行。
        """
        orders: List[OrderEvent] = []
        for ctx in self.strategies:
            orders.extend(self._process_strategy(ctx, bar, signal_cache))
        return orders

    def _process_strategy(
        self, ctx: StrategyContext, bar: BarEvent, signal_cache
    ) -> List[OrderEvent]:
        signals = ctx.get_signal(bar, signal_cache=signal_cache)
        # 风控检查：对每只持仓股票检查止损/止盈/移动止损
        risk_signals = self._check_risks(ctx, bar)

        # 合并并按优先级排序：风控优先，公式按 CLOSE>REDUCE>ADD>OPEN
        all_signals = risk_signals + signals
        all_signals.sort(key=lambda s: _signal_priority(s.signal_type))

        orders: List[OrderEvent] = []
        # 本 bar 已生成全平订单的股票集合：清仓后抑制该股票后续信号
        cleared: set = set()
        for sig in all_signals:
            if sig.stock_code in cleared:
                # 该股票已清仓，跳过后续公式信号（风控信号不会重复，仍跳过）
                continue
            order = self._signal_to_order(ctx, sig, bar)
            if order is None:
                continue
            orders.append(order)
            if sig.signal_type in _FULL_CLOSE_TYPES:
                cleared.add(sig.stock_code)
        return orders

    def _check_risks(self, ctx: StrategyContext, bar: BarEvent) -> List[SignalEvent]:
        """对策略持仓检查止损/止盈/移动止损，返回风控 SignalEvent。"""
        risk_manager = getattr(ctx, "strategy_risk", None)
        if risk_manager is None:
            return []
        risks: List[SignalEvent] = []
        for stock_code, pos in ctx.positions.items():
            if pos.quantity == 0 or stock_code not in bar.stocks:
                continue
            close = bar.stocks[stock_code]["close"]
            if risk_manager.check_stop_loss(pos, close):
                risks.append(SignalEvent(
                    strategy_id=ctx.strategy_id, stock_code=stock_code,
                    signal_name="stop_loss", signal_type=SignalType.STOP_LOSS,
                    bar_time=bar.bar_time,
                ))
            elif risk_manager.check_take_profit(pos, close):
                risks.append(SignalEvent(
                    strategy_id=ctx.strategy_id, stock_code=stock_code,
                    signal_name="take_profit", signal_type=SignalType.TAKE_PROFIT,
                    bar_time=bar.bar_time,
                ))
            elif risk_manager.check_trailing_stop(pos, close):
                risks.append(SignalEvent(
                    strategy_id=ctx.strategy_id, stock_code=stock_code,
                    signal_name="trailing_stop", signal_type=SignalType.TRAILING_STOP,
                    bar_time=bar.bar_time,
                ))
        return risks

    def _signal_to_order(
        self, ctx: StrategyContext, sig: SignalEvent, bar: BarEvent
    ) -> Optional[OrderEvent]:
        """信号转 OrderEvent。SELL 类需有持仓；BUY 类给一个初始量（资金审批在 ExecutionEngine）。"""
        pos = ctx.positions.get(sig.stock_code)
        close = bar.stocks[sig.stock_code]["close"]
        if sig.signal_type in (SignalType.CLOSE, SignalType.STOP_LOSS,
                               SignalType.TAKE_PROFIT, SignalType.TRAILING_STOP):
            # 全平类：需有持仓
            if pos is None or pos.quantity == 0:
                return None
            quantity = pos.quantity
            trade_type = TradeType.SELL
        elif sig.signal_type == SignalType.REDUCE:
            if pos is None or pos.quantity == 0:
                return None
            quantity = int(pos.quantity * Decimal("0.3") / 100) * 100
            if quantity < 100:
                return None
            trade_type = TradeType.SELL
        elif sig.signal_type in (SignalType.OPEN, SignalType.ADD):
            # 买入类：初始量占位，资金审批在 ExecutionEngine 缩减
            quantity = 1000
            trade_type = TradeType.BUY
        else:
            return None
        return OrderEvent(
            strategy_id=ctx.strategy_id,
            portfolio_id=self.portfolio_id,
            stock_code=sig.stock_code,
            trade_type=trade_type,
            signal_type=sig.signal_type,
            quantity=quantity,
            price=close,
            bar_time=bar.bar_time,
        )

    def check_circuit_breaker(self) -> bool:
        return self.risk_manager.circuit_breaker_active

    def snapshot(self, snap_date: date, current_value: Decimal, bar: BarEvent = None) -> dict:
        market_value = Decimal("0")
        if bar is not None:
            for ctx in self.strategies:
                for stock_code, pos in ctx.positions.items():
                    if pos.quantity == 0 or stock_code not in bar.stocks:
                        continue
                    market_value += bar.stocks[stock_code]["close"] * pos.quantity
        return {
            "snap_date": snap_date,
            "total_value": current_value,
            "cash": self.account.cash,
            "market_value": market_value,
        }
