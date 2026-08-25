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
        # 持仓最新价快照（code → close）。BarPoller 逐票完成会发残缺 bar，
        # 实盘分钟 bar 只含本轮新完成的股票；停牌股整天无 bar。估值必须对所有
        # 持仓用最近已知价，不能把缺席持仓当 0（否则组合总市值瞬间塌掉误触发
        # max_drawdown 熔断，见 2026-08-25 对账）。每根 bar 用其有效 close 刷新。
        self._price_snapshot: Dict[str, Decimal] = {}
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
        # §88：熔断期间不清仓，仅暂停新开仓。DEBUG——熔断期每 bar 每 BUY 都会触发，
        # INFO 会刷屏；DEBUG 供排查"为何 BUY 信号没下单"（被剥的 BUY 不进返回列表，
        # 上层 live_engine 的 signal SSE 也不会发，故此处是唯一可见点）。
        if self.risk_manager.is_trading_halted():
            stripped = [o for o in orders if o.trade_type == TradeType.BUY]
            if stripped:
                logger.debug(
                    "portfolio %s 熔断/日内暂停中：剥掉 %d 个 BUY 新开仓 "
                    "(保留 SELL 止损/平仓)：%s",
                    self.portfolio_id, len(stripped),
                    ", ".join("%s/%s" % (o.stock_code, o.signal_name) for o in stripped),
                )
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
            # ADD：需有持仓，且未超 max_add_count。
            # threshold=-1 特殊值 → 跳过 drop 检查（任何价格都加，含上涨/大涨）；
            # 否则现价较成本下跌 ≥ threshold 才加（逢跌加仓，正常取值 1%~20%）。
            # max_add_count 加仓次数上限始终生效（第一道闸）；资金审批/熔断在执行层兜底。
            if pos is None or pos.quantity == 0:
                return None
            if ctx.add_position_threshold != Decimal("-1"):
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

    def _refresh_price_snapshot(self, bar: BarEvent) -> None:
        """用本根 bar 的有效 close(>0) 刷新持仓最新价快照。

        close=0 是 TQ NaN 规整而来（停牌/无数据），不得采用、不得覆盖已有快照。
        """
        for code, ohlcv in bar.stocks.items():
            close = ohlcv.get("close")
            if close is not None and close > 0:
                self._price_snapshot[code] = close

    def _holdings_value(self, ctx: Optional[StrategyContext] = None) -> Decimal:
        """持仓按最近已知价的市值合计（不含现金）。须在 _refresh_price_snapshot 之后调。

        - 有快照价用快照价；从未报过价的回退 avg_cost（持仓成本）；price<=0 同样回退。
        - ctx=None 合计全部策略；否则只算该策略持仓（供回测策略层净值，保证与组合层
          同一口径、Σ策略市值 == 组合市值）。
        """
        market_value = Decimal("0")
        contexts = (ctx,) if ctx is not None else self.strategies
        for c in contexts:
            for code, pos in c.positions.items():
                if pos.quantity == 0:
                    continue
                price = self._price_snapshot.get(code)
                if price is None or price <= 0:
                    price = pos.avg_cost
                market_value += price * pos.quantity
        return market_value

    def holdings_value(self, ctx: StrategyContext) -> Decimal:
        """单策略持仓按最近已知价的市值（回测策略层快照用，口径同 total_value）。"""
        return self._holdings_value(ctx)

    def _refresh_and_value_holdings(self, bar: BarEvent) -> Decimal:
        """刷新价格快照并返回所有持仓市值合计（不含现金）。

        估值口径：对每只持仓取「最近已知价」——本根 bar 带有效 close 的刷新后采用；
        缺席的（残缺 bar / 停牌全天无 bar）沿用快照里上一次的价；从未报过价的回退
        avg_cost。这样 BarPoller 逐票完成的残缺 1m bar 不会让缺席持仓被当 0，总市值连续。
        """
        self._refresh_price_snapshot(bar)
        return self._holdings_value()

    def total_value(self, bar: BarEvent) -> Decimal:
        """组合总市值 = 现金 + 所有策略持仓按最近已知价的市值（回测/实盘共用，审计 #25 去重）。"""
        return self.account.cash + self._refresh_and_value_holdings(bar)

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
        # market_value 复用最近已知价口径（与 total_value 一致）：bar 为 None 时
        # 直接用已积累的价格快照 + avg_cost 兜底，不把缺席持仓当 0。
        if bar is not None:
            self._refresh_price_snapshot(bar)
        market_value = self._holdings_value()
        return {
            "snap_date": snap_date,
            "total_value": current_value,
            "cash": self.account.cash,
            "market_value": market_value,
        }
