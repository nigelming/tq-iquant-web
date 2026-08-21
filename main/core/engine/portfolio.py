from typing import List, Dict, Optional
from decimal import Decimal
from datetime import date
import logging

from .account import Account
from .strategy_context import StrategyContext
from .position import Position
from .risk_manager import PortfolioRiskManager
from .event import BarEvent, SignalEvent, OrderEvent
from tq_iquant_shared.constants import SignalType, TradeType

logger = logging.getLogger(__name__)

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
        cost_params: Optional[Dict] = None,
    ):
        self.portfolio_id = portfolio_id
        self.account = Account(initial_capital)
        self.risk_manager = risk_manager
        self.strategies: List[StrategyContext] = []
        self.benchmark_value: Optional[Decimal] = None
        # 交易成本参数（来自组合表），供 BacktestEngine 构建 dispatcher 时透传
        self.cost_params: Dict = cost_params or {}

    def on_bar(
        self,
        bar: BarEvent,
        signal_cache: Optional[Dict] = None,
        period: Optional[str] = None,
    ) -> List[OrderEvent]:
        """处理一根 bar：取信号 + 风控检查 + 优先级排序 → 返回待执行订单列表。

        订单在下一个 bar 的 open 成交（由 BacktestEngine 调度）。
        信号优先级：风控（止损/止盈/移动止损）> 公式；公式内 CLOSE>REDUCE>ADD>OPEN。
        风控清仓后公式信号不再执行。
        period：C6 按周期节拍驱动——非 None 时只处理 period 相同的策略
        （实盘 5m 边界 bar 不触发 1m 策略，避免风控单串周期）；None=全部（回测/旧调用）。
        """
        orders: List[OrderEvent] = []
        for ctx in self.strategies:
            if period is not None and ctx.period != period:
                continue
            orders.extend(self._process_strategy(ctx, bar, signal_cache))
        # 熔断/日内亏损暂停期间：剥掉新开仓 BUY，保留 SELL（止损/止盈/CLOSE/REDUCE）。
        # §88：熔断期间不清仓，仅暂停新开仓。
        if self.risk_manager.is_trading_halted():
            orders = [o for o in orders if o.trade_type != TradeType.BUY]
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
            # #29：风控未注入不能静默跳过（止损/止盈/移动止损全失效无痕迹）。
            # 告警让失效可见；不 raise 以免中断同组合其他策略的 on_bar。
            logger.warning(
                "strategy %s has no strategy_risk, risk checks skipped",
                ctx.strategy_id,
            )
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
        """信号转 OrderEvent。下单量按策略表参数计算（非硬编码）。
        - 全平类（CLOSE/止损/止盈/移动止损）：量 = 持仓全量
        - REDUCE：量 = 持仓 × reduce_position_ratio
        - OPEN：量 = single_open_ratio × 策略资金 / 价（受 max_positions 约束）
        - ADD：需现价较成本下跌 ≥ add_position_threshold 且 add_count < max_add_count，
               量 = add_position_ratio × 策略资金 / 价
        资金审批（现金/策略上限）仍在 ExecutionEngine 缩减。"""
        pos = ctx.positions.get(sig.stock_code)
        close = bar.stocks[sig.stock_code]["close"]
        # 停牌/无数据 bar：close 经 TQ NaN 规整为 0 → 无法计算下单量（除零）
        # 也无有效成交价 → 跳过该 bar 所有订单
        if close <= Decimal("0"):
            return None
        # 策略资金 = capital_ratio × 组合初始资金
        strategy_fund = ctx.capital_ratio * self.account.initial_capital

        # 主从联动（§89）：从策略 OPEN 只能买主策略当前持有的同一只股票；
        # 主策略清仓（含该股）后从策略不可新开仓。仅约束新开仓（OPEN），
        # ADD/REDUCE/全平类不受约束（存量可自行卖出/加减）。
        if sig.signal_type == SignalType.OPEN and ctx.role == "slave":
            master_ctx = self._find_strategy_by_id(ctx.master_strategy_id)
            if master_ctx is None or not self._has_position(master_ctx, sig.stock_code):
                return None

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
            quantity = int(pos.quantity * ctx.reduce_position_ratio / 100) * 100
            if quantity < 100:
                logger.debug(
                    "REDUCE 拦截:计算量 %d <100（%s 持仓 %d × reduce_ratio %s）",
                    quantity, sig.stock_code, pos.quantity, ctx.reduce_position_ratio,
                )
                return None
            trade_type = TradeType.SELL
        elif sig.signal_type == SignalType.OPEN:
            # OPEN = 开新仓（买入一只当前不持有的股票）。本票已持仓 → 忽略：
            # 加仓是 ADD 的职责（需满足回撤阈值 + max_add_count）。否则公式持续发
            # OPEN（电平信号）会对同一只票每根 bar 加仓一次（实盘 1m 刷屏下单根因）。
            if pos is not None and pos.quantity > 0:
                return None
            # 受 max_positions 约束：已达上限不开新仓
            held = sum(1 for p in ctx.positions.values() if p.quantity > 0)
            if held >= ctx.max_positions:
                logger.debug(
                    "OPEN 拦截:max_positions 已满（持有 %d >= 上限 %d，%s 不开新仓）",
                    held, ctx.max_positions, sig.stock_code,
                )
                return None
            quantity = int(ctx.single_open_ratio * strategy_fund / close / 100) * 100
            if quantity < 100:
                logger.debug(
                    "OPEN 拦截:计算量 %d <100（%s ratio %s × 资金 %s / 价 %s）",
                    quantity, sig.stock_code, ctx.single_open_ratio,
                    strategy_fund, close,
                )
                return None
            trade_type = TradeType.BUY
        elif sig.signal_type == SignalType.ADD:
            # ADD：需有持仓，且现价较成本下跌 ≥ threshold，且未超 max_add_count
            if pos is None or pos.quantity == 0:
                return None
            drop = (pos.avg_cost - close) / pos.avg_cost
            if drop < ctx.add_position_threshold:
                return None
            if pos.add_count >= ctx.max_add_count:
                return None
            quantity = int(ctx.add_position_ratio * strategy_fund / close / 100) * 100
            if quantity < 100:
                logger.debug(
                    "ADD 拦截:计算量 %d <100（%s ratio %s × 资金 %s / 价 %s）",
                    quantity, sig.stock_code, ctx.add_position_ratio,
                    strategy_fund, close,
                )
                return None
            trade_type = TradeType.BUY
        else:
            return None
        return OrderEvent(
            strategy_id=ctx.strategy_id,
            portfolio_id=self.portfolio_id,
            stock_code=sig.stock_code,
            trade_type=trade_type,
            signal_type=sig.signal_type,
            signal_name=sig.signal_name,
            quantity=quantity,
            price=close,
            bar_time=bar.bar_time,
        )

    def check_circuit_breaker(self) -> bool:
        return self.risk_manager.circuit_breaker_active

    def find_strategy(self, strategy_id: int) -> Optional[StrategyContext]:
        """按 strategy_id 在本组合策略列表中找上下文（回测/实盘共用，审计 #25 去重）。

        与 _find_strategy_by_id 同义；后者保留供主从联动内部调用，此为对外公共方法。
        """
        for ctx in self.strategies:
            if ctx.strategy_id == strategy_id:
                return ctx
        return None

    def total_value(self, bar: BarEvent) -> Decimal:
        """组合总市值 = 现金 + 所有策略持仓按当前 close 的市值（回测/实盘共用，审计 #25 去重）。"""
        total = self.account.cash
        for ctx in self.strategies:
            for stock_code, pos in ctx.positions.items():
                if pos.quantity == 0 or stock_code not in bar.stocks:
                    continue
                total += bar.stocks[stock_code]["close"] * pos.quantity
        return total

    def _find_strategy_by_id(self, strategy_id: Optional[int]) -> Optional[StrategyContext]:
        """按 strategy_id 在本组合策略列表中找上下文。"""
        if strategy_id is None:
            return None
        for ctx in self.strategies:
            if ctx.strategy_id == strategy_id:
                return ctx
        return None

    def _has_position(self, ctx: StrategyContext, stock_code: str) -> bool:
        """策略是否持有指定股票（quantity > 0）。用于主从联动约束从策略开仓范围。"""
        pos = ctx.positions.get(stock_code)
        return pos is not None and pos.quantity > 0

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
