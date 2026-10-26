from typing import List, Dict, Optional
from decimal import Decimal
from datetime import date, datetime, time
import logging

from .account import Account
from .strategy_context import StrategyContext
from .position import Position, lot_size_for
from .risk_manager import PortfolioRiskManager
from .event import BarEvent, SignalEvent, OrderEvent
from .decision import NULL_RECORDER
from .format import fmt_money, fmt_pct, fmt_price
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
# 日线及以上周期：日线 bar 的 bar_time 为当日 00:00，close 在收盘后才确认。
# 此类 bar 触发的信号订单须延迟到 t.date() > signal_bar_date 的下一交易日成交，避免未来函数。
_DAILY_PERIODS: set = {"1d", "1w", "1mon"}


def _signal_bar_date_for(period: str, bar: BarEvent) -> Optional[date]:
    """订单的未来函数闸门日期（None = 下一根 bar 即可成交）。

    判别维度 = 触发 bar 时间戳（而非仅策略周期）：日线策略仅在 bar_time 为
    当日 00:00 的日线 bar 触发时打 signal_bar_date（close 未确认，延至次日）；
    分钟 bar 触发的订单（含日线策略的风控/轮动/跟随单）不打——分钟 bar 的
    close 在该 bar 结束时已确认，下一根 bar open 成交严格因果、无未来函数，
    且止损不再无谓延至次日（混合周期回测止损滑点过大，#4）。
    """
    if period in _DAILY_PERIODS and bar.bar_time.time() == time(0, 0):
        return bar.bar_time.date()
    return None


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
        recorder=None,
        position_sizing: str = "dynamic",
    ):
        self.portfolio_id = portfolio_id
        self.account = Account(initial_capital)
        self.risk_manager = risk_manager
        self.strategies: List[StrategyContext] = []
        self.benchmark_value: Optional[Decimal] = None
        # W2 仓位模型：dynamic（默认）按组合当前总净值分配策略资金；fixed 按初始资金。
        self.position_sizing: str = position_sizing
        # 本 bar 组合总净值（on_bar 顶部刷新），供 _strategy_fund；未处理 bar 时为 None。
        self._bar_equity: Optional[Decimal] = None
        # W2 现金管理增强（预留触发点，默认关）：命中阈值仅记决策事件，不产生订单。
        self.cash_enhance_enabled: bool = False
        self.cash_enhance_equity_threshold: Optional[Decimal] = None
        self.cash_enhance_position_ratio_threshold: Optional[Decimal] = None
        self._cash_enhance_last_date = None
        # W3 容量上限（可选，默认 None）：dynamic 资金 = capital_ratio × min(净值, 上限)。
        # 净值超上限后仓位封顶、超额现金闲置（等价"超容量撤回/封盘"），
        # 封堵 20%×增长净值 在长周期复利下开出不可实现的天文仓位。fixed 不受影响。
        self.equity_cap: Optional[Decimal] = None
        # 决策闸门采集器（调参可观测性）：默认空采集器，set_recorder 注入。
        # 同步给 risk_manager（熔断/恢复在其内判定，需同一 recorder）。
        self.recorder = recorder or NULL_RECORDER
        self.risk_manager.recorder = self.recorder
        self.risk_manager.portfolio_id = portfolio_id
        # 持仓最新价快照（code → close）。BarPoller 逐票完成会发残缺 bar，
        # 实盘分钟 bar 只含本轮新完成的股票；停牌股整天无 bar。估值必须对所有
        # 持仓用最近已知价，不能把缺席持仓当 0（否则组合总市值瞬间塌掉误触发
        # max_drawdown 熔断，见 2026-08-25 对账）。每根 bar 用其有效 close 刷新。
        self._price_snapshot: Dict[str, Decimal] = {}
        # 交易成本参数（来自组合表），供 BacktestEngine 构建 dispatcher 时透传
        self.cost_params: Dict = cost_params or {}
        # B5 市场状态闸：当前市场状态标签，由引擎在 on_bar 前注入（None=无状态，
        # ctx.effective_* 全部回落原参数，行为与旧逻辑一致）
        self.current_regime: Optional[str] = None
        # B4 择优轮动：收盘价历史（code → 最近 N 根有效 close）与去重时间戳
        self._price_history: Dict[str, List[Decimal]] = {}
        self._price_hist_last_t: Dict[str, datetime] = {}
        # #1 涨跌停约束（可选，默认关）：开启后回测成交步拦截"涨停买入/跌停卖出"
        # （由 BacktestEngine 判定，仿 no_market_data 延后重试）。类默认 False，
        # 直接构造 Portfolio 的引擎单测不受影响；builder 从组合表字段装配。
        self.price_limit_check_enabled: bool = False
        # #2 成交量/流动性约束（可选，默认关）：单 bar 成交量 × volume_limit_ratio
        # 为成交股数上限（TDX Volume 单位=手，×100 转股数）。超限缩量成交，
        # 不足一手延后重试。同样仅回测成交步生效。
        self.volume_limit_enabled: bool = False
        self.volume_limit_ratio: Decimal = Decimal("0.10")
        # #4 自适应滑点（可选，默认关）：开启后回测成交步按订单量占当根 bar
        # 成交量比例动态加冲击成本（由 BacktestEngine 构造 dispatcher 时透传
        # bar_volumes + 系数）。类默认 False，直接构造 Portfolio 的引擎单测
        # 不受影响；builder 从组合表字段装配。
        self.adaptive_slippage_enabled: bool = False
        self.slippage_impact_coef: Decimal = Decimal("0.10")
        # 费用预留（可选，默认关）：开启后买入资金审批含佣金，防现金为负。
        # 经 setter 同步 account.buy_fee_cfg（见下方 property）。
        self._fee_reserve_enabled: bool = False
        # 可转债 run 级可选开关（仅回测装配，默认关=原行为）：
        # bond_t0_enabled：转债当日买入当日可卖（T+0 回转，股票仍强制 T+1）；
        # bond_lot_enabled：转债最小交易单位 10 张（股票仍 100 股）。
        self.bond_t0_enabled: bool = False
        self.bond_lot_enabled: bool = False

    def set_recorder(self, recorder) -> None:
        """注入决策采集器（回测/实盘引擎在装配后调用），同步给 risk_manager。"""
        self.recorder = recorder or NULL_RECORDER
        self.risk_manager.recorder = self.recorder
        self.risk_manager.portfolio_id = self.portfolio_id

    @property
    def fee_reserve_enabled(self) -> bool:
        return self._fee_reserve_enabled

    @fee_reserve_enabled.setter
    def fee_reserve_enabled(self, enabled: bool) -> None:
        """开关与账户买入费率原子同步：置 True 从 cost_params 写入 buy_fee_cfg
        （缺一不写，保持不启用）；置 False 清回 None（审批恢复仅价×量原行为）。

        与 execution_engine 的费用感知兜底（_fit_buy_qty_to_cash）不冲突：
        前者让 approve_order 审批即含佣（更早、更精确），后者是成交前最终
        兜底钳制（防任何路径遗漏）。"""
        self._fee_reserve_enabled = bool(enabled)
        if enabled:
            min_comm = self.cost_params.get("min_commission")
            buy_rate = self.cost_params.get("buy_commission_rate")
            if min_comm is not None and buy_rate is not None:
                self.account.buy_fee_cfg = (min_comm, buy_rate)
        else:
            self.account.buy_fee_cfg = None

    def _lot(self, stock_code: str) -> int:
        """本组合当前配置下该标的的交易手数（转债 10 张 / 股票 100 股）。"""
        return lot_size_for(stock_code, self.bond_lot_enabled)

    def _rec(self, gate: str, layer: str, action: str, ctx: StrategyContext,
             sig: SignalEvent, bar: BarEvent, **kw) -> None:
        """记录一条信号/风控层闸门事件（带策略/股票/bar 时间公共字段）。"""
        self.recorder.record(
            gate=gate, layer=layer, action=action,
            portfolio_id=self.portfolio_id, strategy_id=ctx.strategy_id,
            stock_code=sig.stock_code, bar_time=bar.bar_time, **kw,
        )

    def on_bar(
        self,
        bar: BarEvent,
        signal_cache: Optional[Dict] = None,
        period: Optional[str] = None,
        inflight_opens: Optional[Dict[int, set]] = None,
    ) -> List[OrderEvent]:
        """处理一根 bar：取信号 + 风控检查 + 优先级排序 → 返回待执行订单列表。

        订单在下一个 bar 的 open 成交（由 BacktestEngine 调度）。
        信号优先级：风控（止损/止盈/移动止损）> 公式；公式内 CLOSE>REDUCE>ADD>OPEN。
        风控清仓后公式信号不再执行。
        period：C6 按周期节拍驱动——非 None 时只处理 period 相同的策略
        （实盘 5m 边界 bar 不触发 1m 策略，避免风控单串周期）；None=全部（回测/旧调用）。
        inflight_opens：strategy_id → 在途未成交 BUY 的股票集合（实盘引擎从
        _pending_orders 注入）。OPEN 闸门把在途单也计入 max_positions；
        回测不传（上一 bar 队列已在成交步全部落账/丢弃），本 bar 内
        排队的 OPEN 由内部累计跟踪。
        """
        # 工作副本：本 bar 处理过程中每排一个 OPEN 就地登记，同 bar 后续信号可见
        inflight: Dict[int, set] = {
            sid: set(codes) for sid, codes in (inflight_opens or {}).items()
        }
        # W2 动态仓位：本 bar 组合总净值（现金 + 持仓按最近已知价市值），
        # _strategy_fund 据此按 capital_ratio 分配策略资金（fixed 模式不使用）。
        self._bar_equity = self.total_value(bar)
        # B4：任一策略开启择优轮动时，逐股票积累收盘价历史供动量计算（未开启零开销
        # 跳过）。close<=0 为停牌/无数据伪影，不采用。cap 取各策略窗口最大值 +2。
        rot_cap = max(
            (c.rotation_momentum_bars for c in self.strategies if c.rotation_enabled),
            default=0,
        )
        if rot_cap:
            self._feed_price_history(bar, rot_cap + 2)
        orders: List[OrderEvent] = []
        for ctx in self.strategies:
            if period is not None and ctx.period != period:
                continue
            strat_orders = self._process_strategy(ctx, bar, signal_cache, inflight)
            orders.extend(strat_orders)
            # follow_master 状态镜像：slave 处理完自身信号后，对"master 已不再持有"的
            # 持仓股全平（本 bar 已自行卖出的股票去重）。以 master 的已成交持仓为准，
            # 故重启安全、跨周期闭环、REDUCE 不触发（master 仍持仓）。
            if ctx.role == "slave" and ctx.slave_exit_mode == "follow_master":
                already_sold = {
                    o.stock_code for o in strat_orders
                    if o.trade_type == TradeType.SELL
                }
                orders.extend(self._follow_master_state(ctx, bar, already_sold))
        # 主从同 bar 时序守卫：slave 的 OPEN 门禁查的是 master 的"已成交"持仓
        # （_has_position），当 master 在本 bar 也决定清仓某股时（其 SELL 尚未成交，
        # 门禁误判 master 仍持有）→ slave 会买入 master 正卖出的同一只股票，违背
        # "从策略只能买入主策略当前持有的同一只股票"。此处按"本 bar 生成的订单"后扫：
        # master 全平某股（CLOSE/止损/止盈/移动止损）→ 剥掉同 bar slave 的 OPEN BUY。
        # 覆盖 master 先于/后于 slave 处理的任意顺序（订单已全部生成，顺序无关）。
        if orders:
            master_full_sells = set()
            for o in orders:
                if (
                    o.trade_type == TradeType.SELL
                    and o.signal_type in _FULL_CLOSE_TYPES
                ):
                    oc = self._find_strategy_by_id(o.strategy_id)
                    if oc is not None and oc.role == "master":
                        master_full_sells.add(o.stock_code)
            if master_full_sells:
                kept: List[OrderEvent] = []
                for o in orders:
                    if (
                        o.trade_type == TradeType.BUY
                        and o.signal_type == SignalType.OPEN
                        and o.stock_code in master_full_sells
                    ):
                        ctx = self._find_strategy_by_id(o.strategy_id)
                        if ctx is not None and ctx.role == "slave":
                            self.recorder.record(
                                gate="slave_master_selling", layer="signal_gate",
                                action="block",
                                portfolio_id=self.portfolio_id,
                                strategy_id=o.strategy_id,
                                stock_code=o.stock_code, bar_time=o.bar_time,
                                param_name="master_strategy_id",
                                param_value=ctx.master_strategy_id,
                                message="从策略 OPEN 拦截：主策略本 bar 同步清仓 %s，"
                                        "禁止同 bar 买入" % o.stock_code,
                            )
                            continue
                    kept.append(o)
                orders = kept
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
                # 区分根因熔断（max_drawdown）还是日内亏损暂停，挂对应调参名
                if self.risk_manager.circuit_breaker_active:
                    p_name, p_val = "max_drawdown", float(self.risk_manager.max_drawdown)
                else:
                    p_name, p_val = "daily_loss_limit", float(self.risk_manager.daily_loss_limit)
                for o in stripped:
                    self.recorder.record(
                        gate="halted_buy_strip", layer="portfolio_risk", action="strip",
                        portfolio_id=self.portfolio_id, strategy_id=o.strategy_id,
                        stock_code=o.stock_code, bar_time=o.bar_time,
                        param_name=p_name, param_value=p_val,
                        requested_qty=o.quantity,
                        message="熔断/日内暂停中剥掉 BUY 新开仓 %s（%s）" % (
                            o.stock_code, o.signal_name),
                    )
            orders = [o for o in orders if o.trade_type != TradeType.BUY]
        return orders

    def _process_strategy(
        self, ctx: StrategyContext, bar: BarEvent, signal_cache,
        inflight: Dict[int, set],
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
            new_orders = self._signal_to_order(ctx, sig, bar, inflight, cleared)
            if not new_orders:
                continue
            orders.extend(new_orders)
            # 全平类 SELL（含轮动 rotation_exit）落单后，抑制该股本 bar 后续信号
            for o in new_orders:
                if o.trade_type == TradeType.SELL and o.signal_type in _FULL_CLOSE_TYPES:
                    cleared.add(o.stock_code)
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
            self.recorder.record(
                gate="missing_strategy_risk", layer="signal_gate", action="block",
                portfolio_id=self.portfolio_id, strategy_id=ctx.strategy_id,
                bar_time=bar.bar_time,
                message="策略 %s 未注入 strategy_risk，止损/止盈/移动止损全部跳过"
                        % ctx.strategy_id,
            )
            return []
        risks: List[SignalEvent] = []
        for stock_code, pos in ctx.positions.items():
            if pos.quantity == 0 or stock_code not in bar.stocks:
                continue
            close = bar.stocks[stock_code]["close"]
            if close > Decimal("0"):
                # 移动止损基准：持仓期间最高价随每根 bar 收盘价刷新（只升不降）
                pos.update_high(close)
            # B5：移动止损阈值按市场状态解析（未开闸/非趋势态回落策略风控原值，行为不变）
            eff_trailing = ctx.effective_trailing_stop(self.current_regime)
            if risk_manager.check_stop_loss(pos, close):
                loss = (pos.avg_cost - close) / pos.avg_cost if pos.avg_cost else Decimal("0")
                risks.append(SignalEvent(
                    strategy_id=ctx.strategy_id, stock_code=stock_code,
                    signal_name="stop_loss", signal_type=SignalType.STOP_LOSS,
                    bar_time=bar.bar_time,
                ))
                self.recorder.record(
                    gate="stop_loss", layer="strategy_risk", action="trigger",
                    portfolio_id=self.portfolio_id, strategy_id=ctx.strategy_id,
                    stock_code=stock_code, bar_time=bar.bar_time,
                    param_name="stop_loss_ratio",
                    param_value=float(risk_manager.stop_loss_ratio),
                    actual_value=float(loss),
                    message="止损：成本 %s 现价 %s 亏 %s" % (
                        fmt_price(pos.avg_cost), fmt_price(close), fmt_pct(loss)),
                )
            elif risk_manager.check_take_profit(pos, close):
                profit = (close - pos.avg_cost) / pos.avg_cost if pos.avg_cost else Decimal("0")
                risks.append(SignalEvent(
                    strategy_id=ctx.strategy_id, stock_code=stock_code,
                    signal_name="take_profit", signal_type=SignalType.TAKE_PROFIT,
                    bar_time=bar.bar_time,
                ))
                self.recorder.record(
                    gate="take_profit", layer="strategy_risk", action="trigger",
                    portfolio_id=self.portfolio_id, strategy_id=ctx.strategy_id,
                    stock_code=stock_code, bar_time=bar.bar_time,
                    param_name="take_profit_ratio",
                    param_value=float(risk_manager.take_profit_ratio),
                    actual_value=float(profit),
                    message="止盈：成本 %s 现价 %s 盈 %s" % (
                        fmt_price(pos.avg_cost), fmt_price(close), fmt_pct(profit)),
                )
            elif risk_manager.check_trailing_stop(pos, close, ratio=eff_trailing):
                drawdown = (
                    (pos.highest_price - close) / pos.highest_price
                    if pos.highest_price else Decimal("0")
                )
                risks.append(SignalEvent(
                    strategy_id=ctx.strategy_id, stock_code=stock_code,
                    signal_name="trailing_stop", signal_type=SignalType.TRAILING_STOP,
                    bar_time=bar.bar_time,
                ))
                self.recorder.record(
                    gate="trailing_stop", layer="strategy_risk", action="trigger",
                    portfolio_id=self.portfolio_id, strategy_id=ctx.strategy_id,
                    stock_code=stock_code, bar_time=bar.bar_time,
                    param_name="trailing_stop_ratio",
                    param_value=float(
                        eff_trailing if eff_trailing is not None
                        else risk_manager.trailing_stop_ratio
                    ),
                    actual_value=float(drawdown),
                    message="移动止损：最高 %s 现价 %s 回撤 %s" % (
                        fmt_price(pos.highest_price), fmt_price(close), fmt_pct(drawdown)),
                )
        return risks

    def maybe_cash_enhancement(self, bar: BarEvent, total_value: Decimal) -> None:
        """现金管理增强（预留触发点）：命中阈值仅记决策事件，不产生任何订单。

        触发条件（OR，阈值非 None 才判）：
        - 组合净资产 total_value ≥ cash_enhance_equity_threshold；
        - 持仓占比 holdings_value / total_value ≤ cash_enhance_position_ratio_threshold。
        每交易日最多记一次（_cash_enhance_last_date 去重）。
        """
        if not self.cash_enhance_enabled:
            return
        day = bar.bar_time.date() if bar.bar_time is not None else None
        if day is not None and self._cash_enhance_last_date == day:
            return
        eq_hit = (
            self.cash_enhance_equity_threshold is not None
            and total_value >= self.cash_enhance_equity_threshold
        )
        ratio_hit = False
        if self.cash_enhance_position_ratio_threshold is not None and total_value > 0:
            ratio = self._holdings_value() / total_value
            ratio_hit = ratio <= self.cash_enhance_position_ratio_threshold
        if not (eq_hit or ratio_hit):
            return
        if day is not None:
            self._cash_enhance_last_date = day
        self.recorder.record(
            gate="cash_enhance_trigger", layer="portfolio_risk", action="trigger",
            portfolio_id=self.portfolio_id,
            bar_time=bar.bar_time,
            actual_value=float(total_value),
            message="现金管理增强为预留接口，待实现",
        )

    def _strategy_fund(self, ctx: StrategyContext) -> Decimal:
        """策略可分配资金（下单量基准）。

        dynamic（默认）：capital_ratio × min(组合当前总净值, 容量上限)（随盈亏伸缩，
        但不超过 equity_cap——超出部分视作已撤回/封盘，不再放大仓位，防止长周期
        复利开出不可实现的天文仓位）；fixed（旧行为）：capital_ratio × 组合初始资金。
        _bar_equity 为 None（未处理 bar，如直接调 _signal_to_order 的单测）→ 回落初始资金。
        """
        if self.position_sizing == "fixed":
            return ctx.capital_ratio * self.account.initial_capital
        equity = self._bar_equity
        if equity is None:
            equity = self.account.initial_capital
        if self.equity_cap is not None:
            equity = min(equity, self.equity_cap)
        return ctx.capital_ratio * equity

    def _signal_to_order(
        self, ctx: StrategyContext, sig: SignalEvent, bar: BarEvent,
        inflight: Optional[Dict[int, set]] = None,
        cleared: Optional[set] = None,
    ) -> List[OrderEvent]:
        """信号转 OrderEvent 列表（B4 轮动一单进两单出）。下单量按策略表参数计算（非硬编码）。
        - 全平类（CLOSE/止损/止盈/移动止损）：量 = 持仓全量
        - REDUCE：量 = 持仓 × reduce_position_ratio
        - OPEN：量 = single_open_ratio × 策略资金 / 价（受 max_positions 约束）
        - ADD：需现价较成本下跌 ≥ add_position_threshold 且 add_count < max_add_count，
               量 = add_position_ratio × 策略资金 / 价
        资金审批（现金/策略上限）仍在 ExecutionEngine 缩减。
        cleared：本 bar 已生成全平订单的股票集合（轮动选轮出候选时排除）。"""
        pos = ctx.positions.get(sig.stock_code)
        close = bar.stocks[sig.stock_code]["close"]
        # 停牌/无数据 bar：close 经 TQ NaN 规整为 0 → 无法计算下单量（除零）
        # 也无有效成交价 → 跳过该 bar 所有订单
        if close <= Decimal("0"):
            self._rec("halted_bar_no_price", "signal_gate", "block", ctx, sig, bar,
                      message="停牌/无数据 bar close=0，无法定价/计算下单量")
            return []
        # 策略资金 = capital_ratio × 组合当前总净值（dynamic）或初始资金（fixed）
        strategy_fund = self._strategy_fund(ctx)

        # 主从联动（§89）：从策略 OPEN 只能买主策略当前持有的同一只股票；
        # 主策略清仓（含该股）后从策略不可新开仓。仅约束新开仓（OPEN），
        # ADD/REDUCE/全平类不受约束（存量可自行卖出/加减）。
        if sig.signal_type == SignalType.OPEN and ctx.role == "slave":
            master_ctx = self._find_strategy_by_id(ctx.master_strategy_id)
            if master_ctx is None or not self._has_position(master_ctx, sig.stock_code):
                self._rec("slave_master_block", "signal_gate", "block", ctx, sig, bar,
                          param_name="master_strategy_id",
                          param_value=ctx.master_strategy_id,
                          message="从策略 OPEN 需主策略持有同一只股票，主策略无持仓")
                return []

        if sig.signal_type in (SignalType.CLOSE, SignalType.STOP_LOSS,
                               SignalType.TAKE_PROFIT, SignalType.TRAILING_STOP):
            # 全平类：需有持仓
            if pos is None or pos.quantity == 0:
                return []
            quantity = pos.quantity
            trade_type = TradeType.SELL
        elif sig.signal_type == SignalType.REDUCE:
            if pos is None or pos.quantity == 0:
                return []
            lot = self._lot(sig.stock_code)
            quantity = int(pos.quantity * ctx.reduce_position_ratio / lot) * lot
            if quantity < lot:
                logger.debug(
                    "REDUCE 拦截:计算量 %d <%d（%s 持仓 %d × reduce_ratio %s）",
                    quantity, lot, sig.stock_code, pos.quantity, ctx.reduce_position_ratio,
                )
                self._rec("reduce_qty_too_small", "signal_gate", "block", ctx, sig, bar,
                          param_name="reduce_position_ratio",
                          param_value=float(ctx.reduce_position_ratio),
                          actual_value=float(quantity),
                          requested_qty=pos.quantity,
                          message="REDUCE 减仓比例 %s × 持仓 %d 向下取整后 %d 股 <%d"
                                  % (fmt_pct(ctx.reduce_position_ratio), pos.quantity,
                                     quantity, lot))
                return []
            trade_type = TradeType.SELL
        elif sig.signal_type == SignalType.OPEN:
            # OPEN = 开新仓（买入一只当前不持有的股票）。本票已持仓 → 忽略：
            # 加仓是 ADD 的职责（需满足回撤阈值 + max_add_count）。否则公式持续发
            # OPEN（电平信号）会对同一只票每根 bar 加仓一次（实盘 1m 刷屏下单根因）。
            if pos is not None and pos.quantity > 0:
                self._rec("open_already_holding", "signal_gate", "block", ctx, sig, bar,
                          message="OPEN 信号时本票已持仓 %d 股（加仓走 ADD），忽略开仓"
                                  % pos.quantity)
                return []
            # 在途单计入（max_positions 超开修复，回测 id7 实测根因）：held 只数已成交
            # 持仓，同 bar 齐发的 OPEN / 实盘 submitted 未成交单不占位 → 超开后闸门
            # 永久拦截。inflight 含本 bar 已排队 + 上层注入的在途未成交 BUY。
            # 在途票若已持有（ADD 在途）不重复占位——held 已含它。
            # 同票在途重复 OPEN 不在此拦：实盘由 _handle_bar 的 inflight_skip 闸拦
            # （同向在途单未确认），回测上一 bar 队列已全部落账/丢弃不会重复。
            inflight_codes = (inflight or {}).get(ctx.strategy_id, set())
            held = sum(1 for p in ctx.positions.values() if p.quantity > 0)
            held_codes = {c for c, p in ctx.positions.items() if p.quantity > 0}
            effective = held + len(inflight_codes - held_codes)
            # B5：槽位上限按市场状态解析（未开闸/非趋势态回落 max_positions，行为不变）
            pos_limit = ctx.effective_max_positions(self.current_regime)
            if effective >= pos_limit:
                # B4：开启择优轮动时，尝试以更强信号轮出最弱持仓腾槽位；
                # 未开启（默认）或轮动条件不满足时按原逻辑拦截
                if ctx.rotation_enabled:
                    return self._try_rotation(ctx, sig, bar, inflight, cleared)
                logger.debug(
                    "OPEN 拦截:max_positions 已满（持有 %d + 在途 %d >= 上限 %d，%s 不开新仓）",
                    held, len(inflight_codes - held_codes), pos_limit, sig.stock_code,
                )
                self._rec("max_positions_full", "signal_gate", "block", ctx, sig, bar,
                          param_name="max_positions",
                          param_value=float(pos_limit),
                          actual_value=float(effective),
                          message="持股数已达上限 %d（持有 %d 只 + 在途 %d 只），%s 不开新仓"
                                  % (pos_limit, held,
                                     len(inflight_codes - held_codes), sig.stock_code))
                return []
            # 本 bar 排队登记：同 bar 后续 OPEN 信号按已占用空位计数
            if inflight is not None:
                inflight.setdefault(ctx.strategy_id, set()).add(sig.stock_code)
            lot = self._lot(sig.stock_code)
            quantity = int(ctx.single_open_ratio * strategy_fund / close / lot) * lot
            if quantity < lot:
                logger.debug(
                    "OPEN 拦截:计算量 %d <%d（%s ratio %s × 资金 %s / 价 %s）",
                    quantity, lot, sig.stock_code, ctx.single_open_ratio,
                    strategy_fund, close,
                )
                self._rec("open_qty_too_small", "capital_gate", "block", ctx, sig, bar,
                          param_name="single_open_ratio",
                          param_value=float(ctx.single_open_ratio),
                          actual_value=float(quantity),
                          message="OPEN 单笔开仓比例 %s × 策略资金 %s / 价 %s 取整后 %d 股 <%d"
                                  % (fmt_pct(ctx.single_open_ratio), fmt_money(strategy_fund),
                                     fmt_price(close), quantity, lot))
                return []
            trade_type = TradeType.BUY
        elif sig.signal_type == SignalType.ADD:
            # ADD：需有持仓，且未超 max_add_count。
            # threshold=-1 特殊值 → 跳过 drop 检查（任何价格都加，含上涨/大涨）；
            # 否则现价较成本下跌 ≥ threshold 才加（逢跌加仓，正常取值 1%~20%）。
            # max_add_count 加仓次数上限始终生效（第一道闸）；资金审批/熔断在执行层兜底。
            if pos is None or pos.quantity == 0:
                return []
            if ctx.add_position_threshold != Decimal("-1"):
                drop = (pos.avg_cost - close) / pos.avg_cost
                if drop < ctx.add_position_threshold:
                    self._rec("add_threshold_not_met", "signal_gate", "block", ctx, sig, bar,
                              param_name="add_position_threshold",
                              param_value=float(ctx.add_position_threshold),
                              actual_value=float(drop),
                              message="ADD 逢跌加仓：现价较成本回撤 %s < 阈值 %s（成本 %s 现价 %s）"
                                      % (fmt_pct(drop), fmt_pct(ctx.add_position_threshold),
                                         fmt_price(pos.avg_cost), fmt_price(close)))
                    return []
            if pos.add_count >= ctx.max_add_count:
                self._rec("add_count_exceeded", "signal_gate", "block", ctx, sig, bar,
                          param_name="max_add_count",
                          param_value=float(ctx.max_add_count),
                          actual_value=float(pos.add_count),
                          message="ADD 加仓次数已达上限 %d（已加 %d 次）"
                                  % (ctx.max_add_count, pos.add_count))
                return []
            lot = self._lot(sig.stock_code)
            quantity = int(ctx.add_position_ratio * strategy_fund / close / lot) * lot
            if quantity < lot:
                logger.debug(
                    "ADD 拦截:计算量 %d <%d（%s ratio %s × 资金 %s / 价 %s）",
                    quantity, lot, sig.stock_code, ctx.add_position_ratio,
                    strategy_fund, close,
                )
                self._rec("add_qty_too_small", "capital_gate", "block", ctx, sig, bar,
                          param_name="add_position_ratio",
                          param_value=float(ctx.add_position_ratio),
                          actual_value=float(quantity),
                          message="ADD 加仓比例 %s × 策略资金 %s / 价 %s 取整后 %d 股 <%d"
                                  % (fmt_pct(ctx.add_position_ratio), fmt_money(strategy_fund),
                                     fmt_price(close), quantity, lot))
                return []
            trade_type = TradeType.BUY
        else:
            return []
        return [OrderEvent(
            strategy_id=ctx.strategy_id,
            portfolio_id=self.portfolio_id,
            stock_code=sig.stock_code,
            trade_type=trade_type,
            signal_type=sig.signal_type,
            signal_name=sig.signal_name,
            quantity=quantity,
            price=close,
            bar_time=bar.bar_time,
            signal_bar_date=_signal_bar_date_for(ctx.period, bar),
            period=bar.period,
        )]

    def _feed_price_history(self, bar: BarEvent, cap: int) -> None:
        """把本根 bar 各股票有效 close(>0) 追加进收盘历史（cap 截断）。

        同一 bar_time 重复喂入（实盘残缺 bar 分轮推送同票）只记一次，防动量失真。
        """
        for code, ohlcv in bar.stocks.items():
            close = ohlcv.get("close")
            if close is None or close <= 0:
                continue
            if self._price_hist_last_t.get(code) == bar.bar_time:
                continue
            self._price_hist_last_t[code] = bar.bar_time
            hist = self._price_history.setdefault(code, [])
            hist.append(close)
            if len(hist) > cap:
                del hist[: len(hist) - cap]

    def _momentum(self, code: str, bars: int) -> Optional[Decimal]:
        """近 bars 根 bar 动量 = 最新收盘 / bars 根前收盘 - 1；历史不足返回 None。"""
        hist = self._price_history.get(code)
        if hist is None or len(hist) < bars + 1:
            return None
        base = hist[-1 - bars]
        if base <= 0:
            return None
        return hist[-1] / base - 1

    def _try_rotation(
        self,
        ctx: StrategyContext,
        sig: SignalEvent,
        bar: BarEvent,
        inflight: Optional[Dict[int, set]],
        cleared: Optional[set],
    ) -> List[OrderEvent]:
        """B4 满槽择优轮动（rotation_enabled 时由 _signal_to_order 满槽分支调用）。

        新 OPEN 信号动量 ≥ 最弱持仓动量 + rotation_hysteresis →
        返回 [全平最弱持仓 SELL(rotation_exit), 新信号股 BUY(OPEN 原信号名)]；
        否则记 rotation_signal_weak 闸门事件并返回 []（不开新仓）。
        动量 = 近 rotation_momentum_bars 根 bar 收盘涨幅（历史不足则该侧不可比）。
        """
        new_mom = self._momentum(sig.stock_code, ctx.rotation_momentum_bars)
        # 候选轮出持仓：有量、非信号股、非本 bar 已清仓、本 bar 有有效价（能卖出）、
        # T+1 当日可卖、动量可算；取动量最小者
        weakest_code: Optional[str] = None
        weakest_mom: Optional[Decimal] = None
        cleared = cleared or set()
        bar_date = bar.bar_time.date()
        for code, p in ctx.positions.items():
            if p.quantity == 0 or code == sig.stock_code or code in cleared:
                continue
            w_close = bar.stocks.get(code, {}).get("close")
            if w_close is None or w_close <= Decimal("0"):
                continue
            if p.available_shares_on(bar_date) < p.quantity:
                continue
            mom = self._momentum(code, ctx.rotation_momentum_bars)
            if mom is None:
                continue
            if weakest_mom is None or mom < weakest_mom:
                weakest_code, weakest_mom = code, mom
        if (
            weakest_code is None
            or new_mom is None
            or new_mom < weakest_mom + ctx.rotation_hysteresis
        ):
            self._rec(
                "rotation_signal_weak", "signal_gate", "block", ctx, sig, bar,
                param_name="rotation_hysteresis",
                param_value=float(ctx.rotation_hysteresis),
                actual_value=float(new_mom) if new_mom is not None else None,
                message="满槽轮动不成立：新信号 %s 动量 %s，最弱持仓 %s 动量 %s（需 ≥ 其 +%s）"
                        % (sig.stock_code,
                           fmt_pct(new_mom) if new_mom is not None else "N/A",
                           weakest_code or "无",
                           fmt_pct(weakest_mom) if weakest_mom is not None else "N/A",
                           fmt_pct(ctx.rotation_hysteresis)),
            )
            return []
        close = bar.stocks[sig.stock_code]["close"]
        strategy_fund = self._strategy_fund(ctx)
        lot = self._lot(sig.stock_code)
        quantity = int(ctx.single_open_ratio * strategy_fund / close / lot) * lot
        if quantity < lot:
            # 新仓计算量不足一手：不换仓（避免为小额仓位平掉整只持仓）
            self._rec("open_qty_too_small", "capital_gate", "block", ctx, sig, bar,
                      param_name="single_open_ratio",
                      param_value=float(ctx.single_open_ratio),
                      actual_value=float(quantity),
                      message="OPEN 单笔开仓比例 %s × 策略资金 %s / 价 %s 取整后 %d 股 <%d"
                              % (fmt_pct(ctx.single_open_ratio), fmt_money(strategy_fund),
                                 fmt_price(close), quantity, lot))
            return []
        weak_pos = ctx.positions[weakest_code]
        weak_close = bar.stocks[weakest_code]["close"]
        signal_bar_date = _signal_bar_date_for(ctx.period, bar)
        self._rec(
            "rotation_replaced", "signal_gate", "trigger", ctx, sig, bar,
            param_name="max_positions",
            param_value=float(ctx.effective_max_positions(self.current_regime)),
            actual_value=float(new_mom),
            message="满槽轮动：%s 动量 %s ≥ 最弱持仓 %s 动量 %s + 滞环 %s，轮出 %d 股换入"
                    % (sig.stock_code, fmt_pct(new_mom), weakest_code,
                       fmt_pct(weakest_mom), fmt_pct(ctx.rotation_hysteresis),
                       weak_pos.quantity),
        )
        # 本 bar 排队登记新码：同 bar 后续 OPEN 按已占位计数
        if inflight is not None:
            inflight.setdefault(ctx.strategy_id, set()).add(sig.stock_code)
        # 实盘说明：虚拟现金按成交回填，SELL 未成交前 BUY 可能因现金不足被审批缩减/
        # 拒绝（已知退化）；下一 bar 信号存续时常规 OPEN 仍可按空出的槽位开仓。
        sell_order = OrderEvent(
            strategy_id=ctx.strategy_id,
            portfolio_id=self.portfolio_id,
            stock_code=weakest_code,
            trade_type=TradeType.SELL,
            signal_type=SignalType.CLOSE,
            signal_name="rotation_exit",
            quantity=weak_pos.quantity,
            price=weak_close,
            bar_time=bar.bar_time,
            signal_bar_date=signal_bar_date,
            period=bar.period,
        )
        buy_order = OrderEvent(
            strategy_id=ctx.strategy_id,
            portfolio_id=self.portfolio_id,
            stock_code=sig.stock_code,
            trade_type=TradeType.BUY,
            signal_type=SignalType.OPEN,
            signal_name=sig.signal_name,
            quantity=quantity,
            price=close,
            bar_time=bar.bar_time,
            signal_bar_date=signal_bar_date,
            period=bar.period,
        )
        return [sell_order, buy_order]

    def _follow_master_state(
        self,
        slave: StrategyContext,
        bar: BarEvent,
        already_sold: set,
    ) -> List[OrderEvent]:
        """follow_master 状态镜像：slave 持有的股票若 master **当前不再持有** → 全平。

        以 master 的已成交持仓为准（重启后由 live_trades 重建），而非 master 本 bar 的
        信号。语义（闭环）：master 清仓某股后，slave 在其**第一个自身周期且该股有价的
        bar** 全平该股。特性：
        - 重启安全：无需事件队列，master 持仓重建即自动补上未执行的跟随；
        - 跨周期闭环：slave 在自身下一 bar 自然检出（无需跨调用状态）；
        - 幂等：清完即止，不重复触发；
        - REDUCE 不触发：master 仅减仓时 quantity 仍 > 0 → 不跟随；仅 master 归零才跟随。
        不影响 slave 本 bar 已自行触发的卖出（already_sold 去重）；无价则留待下一 bar。
        """
        if slave.master_strategy_id is None:
            return []
        master = self._find_strategy_by_id(slave.master_strategy_id)
        if master is None:
            return []
        follow_orders: List[OrderEvent] = []
        for stock_code, pos in slave.positions.items():
            if pos.quantity == 0 or stock_code in already_sold:
                continue
            if self._has_position(master, stock_code):
                continue
            close = bar.stocks.get(stock_code, {}).get("close")
            if close is None or close <= Decimal("0"):
                continue
            follow_orders.append(OrderEvent(
                strategy_id=slave.strategy_id,
                portfolio_id=self.portfolio_id,
                stock_code=stock_code,
                trade_type=TradeType.SELL,
                signal_type=SignalType.CLOSE,
                signal_name="follow_master",
                quantity=pos.quantity,
                price=close,
                bar_time=bar.bar_time,
                signal_bar_date=_signal_bar_date_for(slave.period, bar),
                period=bar.period,
            ))
            self.recorder.record(
                gate="follow_master_sell", layer="signal_gate", action="trigger",
                portfolio_id=self.portfolio_id, strategy_id=slave.strategy_id,
                stock_code=stock_code, bar_time=bar.bar_time,
                param_name="master_strategy_id",
                param_value=slave.master_strategy_id,
                message="follow_master：master 已不再持有 %s，slave 同步全平 %d 股"
                        % (stock_code, pos.quantity),
            )
        return follow_orders

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

    def positions_detail(self, snap_date: date, ctx: Optional[StrategyContext] = None) -> list:
        """当日持仓明细（供回测快照 positions_json；实盘不调用）。

        - ctx=None：组合层，跨策略按 stock_code 聚合（数量/可卖/市值/成本相加）；
          ctx 指定：仅该策略持仓（策略层快照）。
        - 估值价与 total_value/holdings_value 同口径（最近已知价，无效价回退 avg_cost）。
        - sellable_quantity 走 Position T+1 分桶（snap_date 前买入的份额）。
        - Decimal 字段序列化为字符串，保证 json.dumps 安全；按股票代码升序确定输出。
        """
        contexts = (ctx,) if ctx is not None else self.strategies
        agg: Dict[str, dict] = {}
        for c in contexts:
            for code, pos in c.positions.items():
                if pos.quantity == 0:
                    continue
                price = self._price_snapshot.get(code)
                if price is None or price <= 0:
                    price = pos.avg_cost
                slot = agg.get(code)
                if slot is None:
                    slot = {
                        "stock_code": code,
                        "quantity": 0,
                        "sellable_quantity": 0,
                        "market_value": Decimal("0"),
                        "cost_value": Decimal("0"),
                    }
                    agg[code] = slot
                slot["quantity"] += pos.quantity
                slot["sellable_quantity"] += pos.available_shares_on(snap_date)
                slot["market_value"] += price * pos.quantity
                slot["cost_value"] += pos.avg_cost * pos.quantity

        detail = []
        for code in sorted(agg):
            slot = agg[code]
            detail.append({
                "stock_code": slot["stock_code"],
                "quantity": slot["quantity"],
                "sellable_quantity": slot["sellable_quantity"],
                "market_value": str(slot["market_value"]),
                "cost_value": str(slot["cost_value"]),
            })
        return detail

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
            # 当日持仓明细（仅回测落 positions_json；只增字段，实盘不读本键）
            "positions": self.positions_detail(snap_date),
        }
