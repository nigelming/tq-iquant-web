from decimal import Decimal

from datetime import datetime



from core.engine.execution_engine import (

    ExecutionEngine, SimulatedDispatcher, SimulatedT1Checker,

)

from core.engine.account import Account

from core.engine.position import Position

from core.engine.event import OrderEvent

from tq_iquant_shared.constants import TradeType, SignalType





def _order(trade_type, stock_code, quantity, price, bar_time=None):

    return OrderEvent(

        strategy_id=1,

        portfolio_id=1,

        stock_code=stock_code,

        trade_type=trade_type,

        signal_type=SignalType.OPEN,

        quantity=quantity,

        price=Decimal(price),

        bar_time=bar_time or datetime(2026, 7, 30, 15, 0),

    )





def test_place_order_negative_open_returns_none():

    """成交 open 价 ≤0（前复权伪影）→ 不成交，防止负价买入现金倒增。"""

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("-5")})

    order = _order(TradeType.BUY, "000001.SZ", 1000, "10")

    assert dispatcher.place_order(order) is None





def test_approve_order_negative_price_rejected():

    """负价下单（伪影）→ 资金审批拒绝，needed<=cap 对负价恒成立的漏洞封堵。"""

    acc = Account(Decimal("100000"), strategy_capital_limit=Decimal("100000"))

    ok, qty = acc.approve_order(1000, Decimal("-5"), Decimal("0"))

    assert ok is False

    assert qty == 0





def test_execute_buy_approves_and_updates_account_position():

    acc = Account(Decimal("100000"), strategy_capital_limit=Decimal("60000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10.5")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 1000, "10.5",

                   bar_time=datetime(2026, 7, 29, 15, 0))

    trade = engine.execute(order, acc, pos)



    assert trade is not None

    assert trade.quantity == 1000

    assert trade.price == Decimal("10.5")

    # Account 扣款

    assert acc.cash == Decimal("100000") - (trade.amount + trade.commission + trade.stamp_duty)

    # Position 更新

    assert pos.quantity == 1000

    assert pos.avg_cost == Decimal("10.5")

    assert pos.buy_time == datetime(2026, 7, 29, 15, 0)





def test_execute_buy_exceeds_cash_reduces_quantity():

    acc = Account(Decimal("5000"), strategy_capital_limit=Decimal("100000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    # 想买 1000 股 × 10 = 10000，现金 5000 → 缩量。

    # 现金卡满 500 股（5000）后佣金 5 元超支 → 费用感知缩到 400 股（4005≤5000）。

    order = _order(TradeType.BUY, "000001.SZ", 1000, "10")

    trade = engine.execute(order, acc, pos)



    assert trade is not None

    assert trade.quantity == 400

    assert pos.quantity == 400

    assert acc.cash >= Decimal("0")





# ===========================================================================

# #3 策略级敞口上限（可选参数）：策略总持仓市值 ≤ capital_ratio × 初始资金

# ===========================================================================

def test_execute_buy_shrunk_by_strategy_exposure_cap():

    """敞口上限 10000，策略已持市值 6000 → 本单可买额度 4000，1000 股缩至 400 股。"""

    acc = Account(Decimal("100000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 1000, "10")

    trade = engine.execute(

        order, acc, pos,

        strategy_position_value=Decimal("6000"),

        strategy_capital_limit=Decimal("10000"),

    )



    assert trade is not None

    assert trade.quantity == 400

    assert pos.quantity == 400





def test_execute_buy_rejected_when_exposure_cap_remaining_below_one_lot():

    """敞口余量不足一手（上限 10000 - 已持 9600 = 400 < 1000 元/手）→ 拒单。"""

    acc = Account(Decimal("100000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 1000, "10")

    trade = engine.execute(

        order, acc, pos,

        strategy_position_value=Decimal("9600"),

        strategy_capital_limit=Decimal("10000"),

    )



    assert trade is None

    assert pos.quantity == 0





def test_execute_buy_exposure_cap_not_binding_when_cash_smaller():

    """现金 < 敞口余量时按现金缩量（敞口上限不起约束，行为与旧逻辑一致）。

    现金 5000 卡满 500 股后佣金 5 元超支 → 费用感知缩到 400 股（4005≤5000）。"""

    acc = Account(Decimal("5000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 1000, "10")

    trade = engine.execute(

        order, acc, pos,

        strategy_position_value=Decimal("6000"),

        strategy_capital_limit=Decimal("100000"),

    )



    assert trade is not None

    assert trade.quantity == 400

    assert acc.cash >= Decimal("0")





def test_execute_buy_no_exposure_params_keeps_old_behavior():

    """不传敞口参数（默认 None）：不按策略总敞口卡控（实盘/旧调用行为不变）。"""

    acc = Account(Decimal("100000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 1000, "10")

    trade = engine.execute(order, acc, pos)



    assert trade is not None

    assert trade.quantity == 1000





def test_execute_buy_below_one_lot_rejected():

    acc = Account(Decimal("400"), strategy_capital_limit=Decimal("100000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 1000, "10")

    trade = engine.execute(order, acc, pos)



    assert trade is None

    assert pos.quantity == 0

    assert acc.insufficient_count == 1





def test_execute_sell_t1_same_day_blocked():

    acc = Account(Decimal("100000"))

    pos = Position("000001.SZ")

    # 当天买入

    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 30, 9, 30)))

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("11")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    # 同一交易日尝试卖出 → T+1 阻止

    order = _order(TradeType.SELL, "000001.SZ", 1000, "11",

                   bar_time=datetime(2026, 7, 30, 15, 0))

    trade = engine.execute(order, acc, pos)



    assert trade is None

    assert pos.quantity == 1000  # 未卖出





def test_execute_sell_next_day_succeeds():

    acc = Account(Decimal("100000"))

    pos = Position("000001.SZ")

    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("11")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.SELL, "000001.SZ", 1000, "11",

                   bar_time=datetime(2026, 7, 30, 15, 0))

    trade = engine.execute(order, acc, pos)



    assert trade is not None

    assert trade.quantity == 1000

    assert pos.quantity == 0

    # 卖出回款

    assert acc.cash == Decimal("100000") + (trade.amount - trade.commission - trade.stamp_duty)





def _buy(price, quantity, trade_time):

    from core.engine.event import TradeEvent

    return TradeEvent(

        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",

        trade_type=TradeType.BUY, price=Decimal(price), quantity=quantity,

        amount=Decimal(price) * quantity, commission=Decimal("0"),

        stamp_duty=Decimal("0"), trade_time=trade_time,

    )


# ---------------- 可转债：10 张取整 + T+0 回转（可选，默认关）----------------
def test_execute_buy_bond_lot_rounds_to_10():
    """lot_size_of 判定转债 10 张/手：现金 1550、价 100、申请 20 → 成交 10。"""
    acc = Account(Decimal("1550"))
    pos = Position("111024.SH")
    dispatcher = SimulatedDispatcher(open_prices={"111024.SH": Decimal("100")})
    engine = ExecutionEngine(
        dispatcher, SimulatedT1Checker(),
        lot_size_of=lambda code: 10 if code == "111024.SH" else 100,
    )
    order = _order(TradeType.BUY, "111024.SH", 20, "100",
                   bar_time=datetime(2026, 7, 30, 9, 35))
    trade = engine.execute(order, acc, pos)
    assert trade is not None
    assert trade.quantity == 10
    assert pos.quantity == 10


def test_execute_buy_default_lot_unchanged_without_callable():
    """不传 lot_size_of：维持 100 股/手，20 股直接拒单（原行为）。"""
    acc = Account(Decimal("1550"))
    pos = Position("111024.SH")
    dispatcher = SimulatedDispatcher(open_prices={"111024.SH": Decimal("100")})
    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())
    order = _order(TradeType.BUY, "111024.SH", 20, "100",
                   bar_time=datetime(2026, 7, 30, 9, 35))
    assert engine.execute(order, acc, pos) is None
    assert pos.quantity == 0


def test_execute_sell_bond_t0_same_day_fills():
    """T+0 转债：当日 9:35 买入，10:00 SELL 可成交（Position 标记 same_day_sellable）。"""
    acc = Account(Decimal("100000"))
    pos = Position("111024.SH", same_day_sellable=True, lot_size=10)
    pos.apply_trade(_buy("100", 50, datetime(2026, 7, 30, 9, 35)))
    dispatcher = SimulatedDispatcher(open_prices={"111024.SH": Decimal("101")})
    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())
    order = _order(TradeType.SELL, "111024.SH", 50, "101",
                   bar_time=datetime(2026, 7, 30, 10, 0))
    trade = engine.execute(order, acc, pos)
    assert trade is not None
    assert trade.quantity == 50
    assert pos.quantity == 0


def test_fit_buy_qty_to_cash_bond_lot():
    """费用兜底缩量按 10 张：现金 1550、价 100 → 10 张可行；lot=100 → 0。"""
    dispatcher = SimulatedDispatcher(open_prices={})
    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())
    assert engine._fit_buy_qty_to_cash(
        Decimal("100"), Decimal("1550"), 20, 10) == 10
    assert engine._fit_buy_qty_to_cash(
        Decimal("100"), Decimal("1550"), 20, 100) == 0


def test_reduce_by_ratio_bond_lot_10():
    """REDUCE 取整随持仓手数：转债持仓 50 张 × 50% → 20 张；默认 100 股 → 0 不成单。"""
    dispatcher = SimulatedDispatcher(open_prices={})
    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())
    bond = Position("111024.SH", lot_size=10)
    bond.quantity = 50
    order = engine.reduce_by_ratio(bond, Decimal("0.5"))
    assert order is not None and order.quantity == 20
    stock = Position("000001.SZ")
    stock.quantity = 50
    assert engine.reduce_by_ratio(stock, Decimal("0.5")) is None





# ---------------------------------------------------------------------------

# 交易成本参数化（来自组合表，非硬编码）

# ---------------------------------------------------------------------------

def _order_open(stock_code, trade_type, quantity, price, bar_time=None):

    return OrderEvent(

        strategy_id=1,

        portfolio_id=1,

        stock_code=stock_code,

        trade_type=trade_type,

        signal_type=SignalType.OPEN,

        quantity=quantity,

        price=Decimal(price),

        bar_time=bar_time or datetime(2026, 7, 30, 15, 0),

    )





def test_dispatcher_uses_portfolio_cost_params():

    """成本参数从组合表传入：min_commission=10、buy_rate=0.001、sell_rate=0.002、

    stamp_duty_rate=0.001、slippage=0.01。

    BUY 2000@10 → fill=10.1, amt=20200, comm=max(10,20.2)=20.2, stamp=0；

    SELL 2000@10 → fill=9.9, amt=19800, comm=max(10,39.6)=39.6, stamp=19.8。

    """

    dispatcher = SimulatedDispatcher(

        open_prices={"000001.SZ": Decimal("10")},

        min_commission=Decimal("10"),

        buy_commission_rate=Decimal("0.001"),

        sell_commission_rate=Decimal("0.002"),

        stamp_duty_rate=Decimal("0.001"),

        slippage=Decimal("0.01"),

    )

    buy_trade = dispatcher.place_order(

        _order_open("000001.SZ", TradeType.BUY, 2000, "10")

    )

    assert buy_trade.price == Decimal("10.1")

    assert buy_trade.amount == Decimal("10.1") * 2000

    assert buy_trade.commission == Decimal("20.2")

    assert buy_trade.stamp_duty == Decimal("0")



    sell_trade = dispatcher.place_order(

        _order_open("000001.SZ", TradeType.SELL, 2000, "10")

    )

    assert sell_trade.price == Decimal("9.9")

    assert sell_trade.amount == Decimal("9.9") * 2000

    assert sell_trade.commission == Decimal("39.6")

    assert sell_trade.stamp_duty == Decimal("19.8")





def test_dispatcher_min_commission_floor():

    """小单：amt=1000, rate=0.00025 → 算得 0.25 < min_commission=5 → 取 5。"""

    dispatcher = SimulatedDispatcher(

        open_prices={"000001.SZ": Decimal("10")},

        min_commission=Decimal("5"),

        buy_commission_rate=Decimal("0.00025"),

    )

    trade = dispatcher.place_order(

        _order_open("000001.SZ", TradeType.BUY, 100, "10")

    )

    assert trade.commission == Decimal("5")





def test_signal_name_propagates_order_to_trade():

    """OrderEvent.signal_name/signal_type 透传到 TradeEvent（交易明细定位信号来源）。

    公式信号（open_sig·OPEN）与风控信号（stop_loss·STOP_LOSS）两条路径各验一例。"""

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})



    # 公式信号

    formula_order = OrderEvent(

        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",

        trade_type=TradeType.BUY, signal_type=SignalType.OPEN,

        signal_name="open_sig", quantity=100, price=Decimal("10"),

        bar_time=datetime(2026, 7, 30, 15, 0),

    )

    formula_trade = dispatcher.place_order(formula_order)

    assert formula_trade.signal_name == "open_sig"

    assert formula_trade.signal_type == SignalType.OPEN



    # 风控信号

    risk_order = OrderEvent(

        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",

        trade_type=TradeType.SELL, signal_type=SignalType.STOP_LOSS,

        signal_name="stop_loss", quantity=100, price=Decimal("10"),

        bar_time=datetime(2026, 7, 30, 15, 0),

    )

    risk_trade = dispatcher.place_order(risk_order)

    assert risk_trade.signal_name == "stop_loss"

    assert risk_trade.signal_type == SignalType.STOP_LOSS





def test_signal_name_defaults_empty_when_unset():

    """未传 signal_name 的 OrderEvent → TradeEvent.signal_name 为空串（旧调用不破）。"""

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})

    trade = dispatcher.place_order(

        _order_open("000001.SZ", TradeType.BUY, 100, "10")  # 不传 signal_name

    )

    assert trade.signal_name == ""





# ---------------------------------------------------------------------------

# 跳空高开保护：cap_quantity 按信号 bar 收盘价审批，place_order 按下一 bar

# 开盘价成交。跳空高开时实际成本 > 审批金额 → 二次校验缩量或拒单，防止现金为负。

# ---------------------------------------------------------------------------

def test_gap_up_buy_rejected_when_cash_insufficient():

    """跳空高开 + 现金不足 1 手 → 拒单，trade=None，cash 不变。



    收盘 10 审批 100 股（需 1000=现金），次日开盘 11（实际需 1105>1000）→ 拒单。

    """

    acc = Account(Decimal("1000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("11")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 100, "10",

                   bar_time=datetime(2026, 7, 29, 15, 0))

    trade = engine.execute(order, acc, pos)



    assert trade is None

    assert acc.cash == Decimal("1000")  # 现金不变

    assert pos.quantity == 0





def test_gap_up_buy_shrunk_when_partial_cash():

    """跳空高开 + 现金够部分 → 缩量到可用现金能覆盖的 100 股整数倍。



    收盘 10 审批 1000 股（需 10000=现金），次日开盘 11（实际需 11005>10000）→

    缩量到 900 股（900×11=9900+佣金<10000）。

    """

    acc = Account(Decimal("10000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("11")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 1000, "10",

                   bar_time=datetime(2026, 7, 29, 15, 0))

    trade = engine.execute(order, acc, pos)



    assert trade is not None

    assert trade.quantity == 900

    # 现金 = 10000 - (900*11 + commission) > 0（不为负）

    assert acc.cash > Decimal("0")

    assert pos.quantity == 900





def test_normal_buy_no_gap_no_recheck():

    """无跳空（收盘=开盘）→ 正常成交，二次校验不触发（回归测试）。"""

    acc = Account(Decimal("100000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 1000, "10",

                   bar_time=datetime(2026, 7, 29, 15, 0))

    trade = engine.execute(order, acc, pos)



    assert trade is not None

    assert trade.quantity == 1000

    assert trade.price == Decimal("10")

    assert acc.cash == Decimal("100000") - (trade.amount + trade.commission + trade.stamp_duty)





def test_gap_up_buy_secondary_approval_respects_strategy_cap():

    """R2：跳空高开二次审批透传策略敞口上限，不绕过 exposure_cap。



    现金充足（10 万），策略敞口上限 60000，已有持仓市值 55000 → 余量 5000。

    收盘价 10 审批 1000 股 → 首次 cap 缩到 500 股（5000/10）；开盘跳空 11 →

    实际成本 500×11=5500 > 余量 5000 → 二次审批按敞口缩到 400 股（5000//11//100×100）。

    若二次审批未透传 strategy_capital_limit（旧逻辑只按现金 10 万），会保持 500 股

    绕过敞口上限。

    """

    acc = Account(Decimal("100000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("11")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 1000, "10",

                   bar_time=datetime(2026, 7, 29, 15, 0))

    trade = engine.execute(

        order, acc, pos,

        strategy_position_value=Decimal("55000"),

        strategy_capital_limit=Decimal("60000"),

    )



    assert trade is not None

    # 二次审批按敞口缩量：5000/11 → 454 → 100 整数倍 400

    assert trade.quantity == 400

    assert pos.quantity == 400

    # 未绕过敞口：新增市值 400×11=4400 ≤ 余量 5000

    assert Decimal("55000") + Decimal("4400") <= Decimal("60000")





def test_gap_up_buy_secondary_approval_reject_when_cap_exhausted():

    """R2：跳空高开后策略敞口余量不足 1 手 → 二次审批拒单（透传敞口）。"""

    acc = Account(Decimal("100000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("11")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 1000, "10",

                   bar_time=datetime(2026, 7, 29, 15, 0))

    trade = engine.execute(

        order, acc, pos,

        strategy_position_value=Decimal("59900"),

        strategy_capital_limit=Decimal("60000"),

    )



    assert trade is None

    assert pos.quantity == 0





def test_buy_capped_by_cash_reserves_commission_no_negative_cash():

    """费用感知兜底：买入量恰好被现金卡满时，100 股取整尾款 < 佣金会导致

    apply_trade 后现金为负（大额动态仓位下的负现金根因）。修复后应缩量到

    费用可覆盖的最大 100 股整数倍，现金保持 ≥ 0。



    cash=10000 @ 10：approve 允许 1000 股（10000=现金），但佣金 5 元 →

    成本 10005 > 10000 → 缩到 900 股（900×10+5=9005 ≤ 10000）。

    """

    acc = Account(Decimal("10000"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 1000, "10",

                   bar_time=datetime(2026, 7, 29, 15, 0))

    trade = engine.execute(order, acc, pos)



    assert trade is not None

    assert trade.quantity == 900

    assert pos.quantity == 900

    # 现金不为负（10000 - (900×10 + 佣金5) = 995）

    assert acc.cash >= Decimal("0")





def test_buy_fee_overshoot_rejected_when_no_affordable_lot():

    """费用感知兜底：现金卡满后，连一手都无法覆盖佣金 → 拒单（现金不变）。



    cash=1004 @ 10：approve 允许 100 股（1000<1004），但成本 1000+佣金5=1005

    > 1004 → 无可负担手数 → 拒单。

    """

    acc = Account(Decimal("1004"))

    pos = Position("000001.SZ")

    dispatcher = SimulatedDispatcher(open_prices={"000001.SZ": Decimal("10")})

    engine = ExecutionEngine(dispatcher, SimulatedT1Checker())



    order = _order(TradeType.BUY, "000001.SZ", 100, "10",

                   bar_time=datetime(2026, 7, 29, 15, 0))

    trade = engine.execute(order, acc, pos)



    assert trade is None

    assert acc.cash == Decimal("1004")

    assert pos.quantity == 0


# ===========================================================================
# #4 自适应滑点（可选）：bar_volumes + slippage_impact_coef 均具备时按订单量
# 占 bar 成交量比例加冲击成本；否则回退固定滑点（原行为）
# ===========================================================================
def test_adaptive_slippage_off_by_default_fixed_rate():
    """默认不传 bar_volumes/系数：固定滑点（原行为不变）。"""
    dispatcher = SimulatedDispatcher(
        open_prices={"000001.SZ": Decimal("10")}, slippage=Decimal("0.001"))
    trade = dispatcher.place_order(_order(TradeType.BUY, "000001.SZ", 2000, "10"))
    assert trade.price == Decimal("10") * (Decimal("1") + Decimal("0.001"))


def test_adaptive_slippage_scales_with_order_volume_share():
    """开启：有效滑点 = 基础 + 系数×股数/量（量=股）。量10000股、单2000股
    → 冲击 0.10×0.2=0.02，基础 0.001 → 成交价 10×1.021。"""
    dispatcher = SimulatedDispatcher(
        open_prices={"000001.SZ": Decimal("10")}, slippage=Decimal("0.001"),
        bar_volumes={"000001.SZ": 10000}, slippage_impact_coef=Decimal("0.10"))
    trade = dispatcher.place_order(_order(TradeType.BUY, "000001.SZ", 2000, "10"))
    assert trade.price == Decimal("10") * Decimal("1.021")


def test_adaptive_slippage_sell_direction_downward():
    """SELL 方向下浮：同口径 10×(1-0.021)。"""
    dispatcher = SimulatedDispatcher(
        open_prices={"000001.SZ": Decimal("10")}, slippage=Decimal("0.001"),
        bar_volumes={"000001.SZ": 10000}, slippage_impact_coef=Decimal("0.10"))
    trade = dispatcher.place_order(_order(TradeType.SELL, "000001.SZ", 2000, "10"))
    assert trade.price == Decimal("10") * Decimal("0.979")


def test_adaptive_slippage_missing_volume_falls_back_to_base():
    """bar 无该票成交量记录 → 回退基础滑点。"""
    dispatcher = SimulatedDispatcher(
        open_prices={"000001.SZ": Decimal("10")}, slippage=Decimal("0.001"),
        bar_volumes={"000002.SZ": 500}, slippage_impact_coef=Decimal("0.10"))
    trade = dispatcher.place_order(_order(TradeType.BUY, "000001.SZ", 2000, "10"))
    assert trade.price == Decimal("10") * Decimal("1.001")


def test_adaptive_slippage_zero_volume_falls_back_to_base():
    """bar 成交量为 0 → 回退基础滑点（防除零）。"""
    dispatcher = SimulatedDispatcher(
        open_prices={"000001.SZ": Decimal("10")}, slippage=Decimal("0.001"),
        bar_volumes={"000001.SZ": 0}, slippage_impact_coef=Decimal("0.10"))
    trade = dispatcher.place_order(_order(TradeType.BUY, "000001.SZ", 2000, "10"))
    assert trade.price == Decimal("10") * Decimal("1.001")


def test_adaptive_slippage_capped_at_10pct():
    """冲击过大时有效滑点封顶 0.10（防极端失真）：量10股、单2000股、
    系数 1.0 → 冲击巨大 → 封顶 0.10。"""
    dispatcher = SimulatedDispatcher(
        open_prices={"000001.SZ": Decimal("10")}, slippage=Decimal("0.05"),
        bar_volumes={"000001.SZ": 10}, slippage_impact_coef=Decimal("1.0"))
    trade = dispatcher.place_order(_order(TradeType.BUY, "000001.SZ", 2000, "10"))
    assert trade.price == Decimal("10") * Decimal("1.10")
