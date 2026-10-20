from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import Optional
import math

from .event import OrderEvent, TradeEvent
from .account import Account
from .position import Position
from .decision import NULL_RECORDER
from .format import fmt_money, fmt_price
from tq_iquant_shared.constants import TradeType, SignalType


class OrderDispatcher(ABC):
    @abstractmethod
    def place_order(self, order: OrderEvent) -> Optional[TradeEvent]:
        ...

    def fill_price(self, order: OrderEvent) -> Optional[Decimal]:
        """成交价（若可知）。默认 None=不重校验。回测 SimulatedDispatcher 覆盖。"""
        return None


class T1Checker(ABC):
    @abstractmethod
    def get_available_shares(self, position: Position, query_date: date) -> int:
        ...


class SimulatedDispatcher(OrderDispatcher):
    def __init__(
        self,
        open_prices: dict = None,
        *,
        min_commission: Decimal = Decimal("5"),
        buy_commission_rate: Decimal = Decimal("0.00025"),
        sell_commission_rate: Decimal = Decimal("0.00025"),
        stamp_duty_rate: Decimal = Decimal("0.0005"),
        slippage: Decimal = Decimal("0"),
        bar_volumes: dict = None,
        slippage_impact_coef: Optional[Decimal] = None,
    ):
        """成本参数来自组合表，默认值 = 历史硬编码值，老调用不破。

        bar_volumes / slippage_impact_coef：#4 自适应滑点（可选）。两者均非 None
        时有效滑点 = slippage + coef×订单股数/(bar成交量×100)，封顶 0.10；
        任一缺失或该票无量时回退固定 slippage（原行为）。bar_volumes 值为
        TDX Volume（单位=手，×100 转股数）。
        """
        self.open_prices = open_prices or {}
        self.min_commission = min_commission
        self.buy_commission_rate = buy_commission_rate
        self.sell_commission_rate = sell_commission_rate
        self.stamp_duty_rate = stamp_duty_rate
        self.slippage = slippage
        self.bar_volumes = bar_volumes
        self.slippage_impact_coef = slippage_impact_coef

    def _effective_slippage(self, order: OrderEvent) -> Decimal:
        """#4 自适应滑点：市场冲击随订单量占 bar 成交量比例线性上浮，封顶 0.10。
        bar_volumes 值为 TQ Volume（单位=股），直接按股数占比计算。"""
        if self.bar_volumes is None or self.slippage_impact_coef is None:
            return self.slippage
        vol = self.bar_volumes.get(order.stock_code)
        if not vol:
            return self.slippage
        impact = (
            self.slippage_impact_coef
            * Decimal(order.quantity)
            / Decimal(int(vol))
        )
        return min(self.slippage + impact, Decimal("0.10"))

    def fill_price(self, order: OrderEvent) -> Optional[Decimal]:
        """含滑点的成交价，与 place_order 的定价算法一致；无价则 None。"""
        price = self.open_prices.get(order.stock_code)
        if not price:
            return None
        if order.trade_type.value == "BUY":
            return price * (Decimal("1") + self.slippage)
        return price * (Decimal("1") - self.slippage)

    def place_order(self, order: OrderEvent) -> Optional[TradeEvent]:
        price = self.open_prices.get(order.stock_code)
        # 非正成交价（前复权伪影/停牌无价）不成交：负价买入会让现金倒增、股数失真
        if not price or price <= 0:
            return None
        # 滑点：买入成交价上浮、卖出成交价下浮（#4 开启时按量动态上浮）
        slippage = self._effective_slippage(order)
        if order.trade_type.value == "BUY":
            fill_price = price * (Decimal("1") + slippage)
        else:
            fill_price = price * (Decimal("1") - slippage)
        amount = fill_price * order.quantity
        is_sell = order.trade_type.value == "SELL"
        rate = self.sell_commission_rate if is_sell else self.buy_commission_rate
        commission = max(self.min_commission, amount * rate)
        stamp_duty = amount * self.stamp_duty_rate if is_sell else Decimal("0")
        return TradeEvent(
            strategy_id=order.strategy_id,
            portfolio_id=order.portfolio_id,
            stock_code=order.stock_code,
            trade_type=order.trade_type,
            price=fill_price,
            quantity=order.quantity,
            amount=amount,
            commission=commission,
            stamp_duty=stamp_duty,
            trade_time=order.bar_time,
            signal_type=order.signal_type,
            signal_name=order.signal_name,
        )


class SimulatedT1Checker(T1Checker):
    def get_available_shares(self, position: Position, query_date: date) -> int:
        # T+1：query_date 之前买入的份额可卖，当日买入不可卖
        return position.available_shares_on(query_date)


class LiveT1Checker(T1Checker):
    """实盘 T+1 检查：以桥 /positions 的 available（m_nCanUseVolume）为 SELL 上限。

    LiveEngine 每 bar 刷一次 _available_map（一次 /positions HTTP），
    get_available_shares 取 min(本策略持有量, 桥可用)——Core 账面虚高时不再超发 SELL。
    桥无该仓/未取到 → 全量放行（券商端 T+1 兜底，G6 处理拒单；避免误伤正常卖出）。
    """

    def __init__(self):
        self._available_map = {}

    def set_available_map(self, available_map: dict) -> None:
        self._available_map = available_map or {}

    def get_available_shares(self, position: Position, query_date: date) -> int:
        avail = self._available_map.get(position.stock_code)
        if avail is None:
            return position.quantity
        return min(position.quantity, avail)

    def consume_available(self, stock_code: str, quantity: int) -> None:
        """F6：SELL 下单成功后从 bar 可用量扣减（同 bar 后续 SELL 见递减后的值）。

        每 bar _refresh_available_map 重设快照（一次 /positions），本 bar 内按成交顺序
        递减——A 卖 600 + B 卖 400、available 800 → B 只见 200，避免同 bar 多策略超卖
        （券商拒掉超出的部分）。只在下单成功（桥受理）后调用，拒单/失败不扣。
        """
        avail = self._available_map.get(stock_code)
        if avail is None:
            return
        self._available_map[stock_code] = max(0, avail - quantity)


class ExecutionEngine:
    def __init__(self, dispatcher: OrderDispatcher, t1_checker: T1Checker,
                 recorder=None, lot_size_of=None):
        self._dispatcher = dispatcher
        self._t1_checker = t1_checker
        # 决策闸门采集（调参可观测性）：默认空采集器，回测/实盘引擎注入同一实例
        self.recorder = recorder or NULL_RECORDER
        # 交易手数回调（可选，默认 None=恒 100 股，原行为）。
        # 可转债 10 张取整开启时由回测引擎注入 lot_size_for(code, bond_lot_enabled)。
        self._lot_size_of = lot_size_of

    def _lot(self, stock_code: str) -> int:
        if self._lot_size_of is None:
            return 100
        return self._lot_size_of(stock_code)

    def _rec_gate(self, gate, action, order, layer, *, requested=None, final=None,
                  param_name=None, param_value=None, actual_value=None, message=None):
        self.recorder.record(
            gate=gate, layer=layer, action=action,
            portfolio_id=order.portfolio_id or 0,
            strategy_id=order.strategy_id,
            stock_code=order.stock_code,
            bar_time=order.bar_time,
            requested_qty=requested, final_qty=final,
            param_name=param_name, param_value=param_value,
            actual_value=actual_value, message=message,
        )

    def cap_quantity(
        self,
        order: OrderEvent,
        account: Account,
        position: Optional[Position],
        *,
        strategy_position_value: Optional[Decimal] = None,
        strategy_capital_limit: Optional[Decimal] = None,
    ) -> Optional[int]:
        """审批/T+1 量上限：返回最终下单量（None=不通过不下单）。

        BUY：account.approve_order（策略持仓上限 + 组合现金）；
        SELL：t1_checker 可用股数（回测 Simulated 按日分桶 / 实盘桥 available）。
        execute 与 LiveEngine._handle_bar（切片5 落 submitted 前）共用，保证 DB
        下单量与实发一致。

        #3 策略级敞口上限（可选，默认 None 不启用）：strategy_capital_limit 非 None
        时 BUY 可买额度 = min(现金, 上限 - strategy_position_value)——策略总持仓市值
        ≤ capital_ratio × 初始资金。缩量/拒单记 exposure_cap 闸门事件（敞口为 binding
        约束时），否则沿用 insufficient_funds/order_shrunk（现金为 binding 约束）。
        """
        if order.trade_type.value == "BUY":
            lot = self._lot(order.stock_code)
            use_exposure = strategy_capital_limit is not None
            pos_value = (
                (strategy_position_value or Decimal("0")) if use_exposure
                else (position.market_value if position else Decimal("0"))
            )
            approved, qty = account.approve_order(
                order.quantity, order.price or Decimal("0"), pos_value,
                strategy_capital_limit_override=strategy_capital_limit if use_exposure else None,
                lot_size=lot,
            )
            # binding 约束判定：敞口余量 < 现金 → 缩量/拒单归因于策略敞口上限
            exposure_binds = use_exposure and (
                strategy_capital_limit - pos_value < account.cash
            )
            if not approved or qty < lot:
                # 资金不足：不足 1 手拒单（account.insufficient_count 已在 approve_order 内自增）
                if exposure_binds:
                    self._rec_gate(
                        "exposure_cap", "reject", order, "capital_gate",
                        requested=order.quantity, final=0,
                        param_name="capital_ratio", param_value=float(strategy_capital_limit),
                        actual_value=float(pos_value),
                        message="策略敞口上限拒单：策略持仓市值 %s + 新单超出上限 %s"
                                "（capital_ratio × 初始资金），缩量后不足 1 手"
                                % (fmt_money(pos_value), fmt_money(strategy_capital_limit)),
                    )
                else:
                    self._rec_gate(
                        "insufficient_funds", "reject", order, "capital_gate",
                        requested=order.quantity, final=0,
                        param_name="cash", actual_value=float(account.cash),
                        message="资金不足拒单：需 %s 元买 %d 股 @%s，可用现金 %s（缩量后 %d 股<1手）"
                                % (fmt_money(order.price * order.quantity if order.price else 0),
                                   order.quantity, fmt_price(order.price),
                                   fmt_money(account.cash), qty),
                    )
                return None
            if qty < order.quantity:
                # 资金够 1 手但不足额 → 缩量
                if exposure_binds:
                    self._rec_gate(
                        "exposure_cap", "shrink", order, "capital_gate",
                        requested=order.quantity, final=qty,
                        param_name="capital_ratio", param_value=float(strategy_capital_limit),
                        actual_value=float(pos_value),
                        message="策略敞口上限缩量：请求 %d 股，策略持仓市值 %s，上限 %s"
                                "（capital_ratio × 初始资金），缩至 %d 股 @%s"
                                % (order.quantity, fmt_money(pos_value),
                                   fmt_money(strategy_capital_limit), qty,
                                   fmt_price(order.price)),
                    )
                else:
                    self._rec_gate(
                        "order_shrunk", "shrink", order, "capital_gate",
                        requested=order.quantity, final=qty,
                        param_name="cash", actual_value=float(account.cash),
                        message="资金不足缩量：请求 %d 股，按可用现金 %s 缩至 %d 股 @%s"
                                % (order.quantity, fmt_money(account.cash), qty, fmt_price(order.price)),
                    )
            return qty
        if position is None or order.bar_time is None:
            return None
        available = self._t1_checker.get_available_shares(
            position, order.bar_time.date()
        )
        qty = min(order.quantity, available)
        # SELL 手数随持仓（建仓时按标的类型定档：转债 10 张 / 股票 100 股）
        sell_lot = position.lot_size
        if qty < sell_lot:
            self._rec_gate(
                "t1_insufficient", "block", order, "t1",
                requested=order.quantity, final=qty,
                param_name="available_shares", actual_value=float(available),
                message="T+1 可卖不足：请求 %d 股，可用 %d 股，可成交量 %d <%d"
                        % (order.quantity, available, qty, sell_lot),
            )
            return None
        if qty < order.quantity:
            self._rec_gate(
                "t1_clamp", "clamp", order, "t1",
                requested=order.quantity, final=qty,
                param_name="available_shares", actual_value=float(available),
                message="T+1 可卖钳量：请求 %d 股，可用 %d 股，实卖 %d 股"
                        % (order.quantity, available, qty),
            )
        return qty

    def _fit_buy_qty_to_cash(
        self, price: Decimal, cash: Decimal, quantity: int,
        lot_size: int = 100,
    ) -> int:
        """费用感知缩量：返回 ≤ quantity 的最大整手倍数，使
        成交额 + 佣金 ≤ 可用现金（approve_order 只卡 price×qty，未预留佣金，
        大额买入在取整尾款 < 手续费时会透支现金）。不足一手返回 0。

        lot_size（可选，默认 100=股票原行为）：可转债 10 张取整时传 10。

        只需检查 quantity 与 quantity-lot_size：每缩一手省下的金额
        远大于该手的佣金，故最大可行量只可能是这两者之一。
        """
        if price <= 0 or cash <= 0 or quantity < lot_size:
            return 0
        buy_rate = getattr(
            self._dispatcher, "buy_commission_rate", Decimal("0.00025")
        )
        min_comm = getattr(self._dispatcher, "min_commission", Decimal("5"))
        for q in (quantity, quantity - lot_size):
            if q < lot_size:
                continue
            amount = price * q
            commission = max(min_comm, amount * buy_rate)
            if amount + commission <= cash:
                return q
        return 0

    def execute(
        self,
        order: OrderEvent,
        account: Account,
        position: Optional[Position],
        apply: bool = True,
        *,
        strategy_position_value: Optional[Decimal] = None,
        strategy_capital_limit: Optional[Decimal] = None,
    ) -> Optional[TradeEvent]:
        """执行订单。

        apply=True（回测/旧实盘）：place_order 成功后立即 apply_trade 更新账户持仓。
        apply=False（切片5 实盘）：只审批 + 发单，不 apply_trade——成交回报轮询回填
        确认 filled 后才由 LiveEngine._apply_filled_trade 更新（submitted 阶段不 apply，
        避免受理即成交的近似；I4 崩溃窗口下 DB 有 submitted 记录即可）。

        #3 策略级敞口上限（可选）：两个 strategy_* 参数透传 cap_quantity，默认 None
        不启用（实盘 LiveEngine 不传 → 行为不变）。
        """
        capped = self.cap_quantity(
            order, account, position,
            strategy_position_value=strategy_position_value,
            strategy_capital_limit=strategy_capital_limit,
        )
        if capped is None:
            return None
        order.quantity = capped

        trade = self._dispatcher.place_order(order)
        if not trade:
            return None

        # 跳空高开保护：BUY 实际成交成本（含滑点+佣金+印花税）超可用现金 →
        # 用实际成交价 trade.price（已含滑点）重新审批，缩量到整手或拒单。
        # 原因：cap_quantity 用信号 bar 收盘价审批，place_order 用下一 bar 开盘价成交，
        # 跳空高开时实际成本 > 审批金额，不加此守卫则 deduct_cash 后现金为负。
        if apply and order.trade_type == TradeType.BUY:
            buy_lot = self._lot(order.stock_code)
            cost = trade.amount + trade.commission + trade.stamp_duty
            # R2：二次审批触发条件 = 实际成本超现金 或 超策略敞口余量（capital_ratio ×
            # 初始资金 − 策略持仓市值）。跳空高开后实际成交价 > 审批价，即使现金充足，
            # 也可能突破策略级敞口上限——不加该条件则 exposure_cap 被绕过。
            strategy_remaining = (
                strategy_capital_limit - (strategy_position_value or Decimal("0"))
                if strategy_capital_limit is not None else None
            )
            if cost > account.cash or (
                strategy_remaining is not None and cost > strategy_remaining
            ):
                # R2：二次审批透传策略敞口上限——跳空高开后实际成本 > 审批金额，
                # 若只按现金重批会绕过策略级敞口上限（capital_ratio × 初始资金），
                # 缩量/拒单归因也随之失真。strategy_capital_limit 为 None（不启用
                # 策略敞口）时行为与原来一致。
                pos_value = (
                    (strategy_position_value or Decimal("0"))
                    if strategy_capital_limit is not None
                    else (position.market_value if position else Decimal("0"))
                )
                approved, qty = account.approve_order(
                    order.quantity, trade.price,
                    pos_value,
                    strategy_capital_limit_override=strategy_capital_limit,
                    lot_size=buy_lot,
                )
                if not approved or qty < buy_lot:
                    self._rec_gate(
                        "insufficient_funds", "reject", order, "capital_gate",
                        requested=order.quantity, final=0,
                        param_name="cash", actual_value=float(account.cash),
                        message="跳空高开资金不足：成交价 %s，需 %s，可用 %s"
                                % (fmt_price(trade.price), fmt_money(cost), fmt_money(account.cash)),
                    )
                    return None
                if qty < order.quantity:
                    self._rec_gate(
                        "order_shrunk", "shrink", order, "capital_gate",
                        requested=order.quantity, final=qty,
                        param_name="cash", actual_value=float(account.cash),
                        message="跳空高开缩量：%d → %d @ %s"
                                % (order.quantity, qty, fmt_price(trade.price)),
                    )
                    order.quantity = qty
                    trade = self._dispatcher.place_order(order)
                    if not trade:
                        return None

        if apply:
            # 费用感知最终兜底：approve_order 未预留佣金，现金被卡满 + 取整尾款
            # < 手续费时仍会透支（大额动态仓位下可见负现金）。缩到
            # 费用可覆盖的最大整手倍数；不足一手拒单。
            if order.trade_type == TradeType.BUY:
                fee_lot = self._lot(order.stock_code)
                cost = trade.amount + trade.commission + trade.stamp_duty
                if cost > account.cash:
                    fit_qty = self._fit_buy_qty_to_cash(
                        trade.price, account.cash, order.quantity, fee_lot
                    )
                    if fit_qty < fee_lot:
                        self._rec_gate(
                            "insufficient_funds", "reject", order, "capital_gate",
                            requested=order.quantity, final=0,
                            param_name="cash", actual_value=float(account.cash),
                            message="买入成本（含佣金）超可用现金：需 %s，可用 %s"
                                    % (fmt_money(cost), fmt_money(account.cash)),
                        )
                        return None
                    if fit_qty < order.quantity:
                        self._rec_gate(
                            "order_shrunk", "shrink", order, "capital_gate",
                            requested=order.quantity, final=fit_qty,
                            param_name="cash", actual_value=float(account.cash),
                            message="买入佣金超出尾款，缩量 %d → %d @ %s"
                                    % (order.quantity, fit_qty, fmt_price(trade.price)),
                        )
                        order.quantity = fit_qty
                        trade = self._dispatcher.place_order(order)
                        if not trade:
                            return None
                        cost = trade.amount + trade.commission + trade.stamp_duty
            # 统一用 apply_trade 更新账户和持仓
            account.apply_trade(trade)
            if position is not None:
                position.apply_trade(trade)

        return trade

    def reduce_by_ratio(self, position: Position, ratio: Decimal) -> Optional[OrderEvent]:
        if position.quantity == 0:
            return None
        # 按持仓手数取整（股票 100 股 / 可转债 10 张）；不足一手不产生 REDUCE
        lot = position.lot_size
        qty = int(position.quantity * ratio / lot) * lot
        if qty < lot:
            return None
        return OrderEvent(
            strategy_id=0,
            portfolio_id=0,
            stock_code=position.stock_code,
            trade_type=TradeType.SELL,
            signal_type=SignalType.REDUCE,
            quantity=qty,
            price=None,
            bar_time=None,
        )
