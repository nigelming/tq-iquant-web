from datetime import datetime
from decimal import Decimal

from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import StrategyRiskManager, PortfolioRiskManager
from core.engine.position import Position
from core.engine.event import BarEvent
from tq_iquant_shared.constants import SignalType, TradeType


def _bar(stock_code, close, bar_time):
    return BarEvent(
        stocks={stock_code: {
            "open": Decimal("10"), "high": Decimal(str(close)),
            "low": Decimal("9"), "close": Decimal(str(close)), "volume": 1000,
        }},
        bar_time=bar_time,
    )


def _portfolio_with_strategy(stock_code, formula_signals, stop_loss=Decimal("0.1")):
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5,
    )
    ctx.formula_signals = formula_signals
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=stop_loss,
        take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)
    return port, ctx


def test_on_bar_close_signal_produces_sell_order():
    """持仓中 + CLOSE 信号 → 产出 SELL OrderEvent。"""
    port, ctx = _portfolio_with_strategy(
        "000001.SZ",
        [{"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": 1}],
    )
    # 预置持仓
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos

    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "close_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)

    assert len(orders) == 1
    assert orders[0].trade_type == TradeType.SELL
    assert orders[0].stock_code == "000001.SZ"
    assert orders[0].quantity == 1000  # CLOSE 全平


def test_on_bar_open_signal_produces_buy_order():
    """无持仓 + OPEN 信号 → 产出 BUY OrderEvent。"""
    port, ctx = _portfolio_with_strategy(
        "000001.SZ",
        [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}],
    )
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)

    assert len(orders) == 1
    assert orders[0].trade_type == TradeType.BUY
    assert orders[0].stock_code == "000001.SZ"


def test_on_bar_zero_close_skips_order():
    """停牌/无数据 bar 收盘价=0（TQ NaN 规整而来）→ 不下单（避免 DivisionByZero）。
    真机 5m 场景：停牌 bar OHLC 为 NaN，转换层规整为 0，公式可能仍输出信号，
    但 close=0 无法计算下单量（除零）也无有效成交价 → 跳过该 bar 所有订单。"""
    port, ctx = _portfolio_with_strategy(
        "000001.SZ",
        [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}],
    )
    # close=0 模拟停牌 bar
    bar = BarEvent(
        stocks={"000001.SZ": {
            "open": Decimal("0"), "high": Decimal("0"),
            "low": Decimal("0"), "close": Decimal("0"), "volume": 0,
        }},
        bar_time=datetime(2026, 7, 30, 13, 5),
    )
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    assert orders == []  # close=0 不下单，不抛 DivisionByZero


def test_on_bar_stop_loss_prioritized_over_formula_close():
    """止损风控信号优先于公式 CLOSE 信号。持仓亏损触发止损 + 同时有 CLOSE 信号 →
    风控先执行（清仓后公式信号不再执行），只产 1 个 SELL。"""
    port, ctx = _portfolio_with_strategy(
        "000001.SZ",
        [{"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": 1}],
        stop_loss=Decimal("0.05"),  # 5% 止损
    )
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos

    # 当前价 9.4 → 亏损 6% > 5% 止损触发
    bar = _bar("000001.SZ", "9.4", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "close_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)

    assert len(orders) == 1
    assert orders[0].trade_type == TradeType.SELL
    assert orders[0].signal_type == SignalType.STOP_LOSS


def test_on_bar_signal_priority_close_over_open():
    """同策略内 CLOSE 优先于 OPEN（按 §5.3.2 第8g条排序）。"""
    port, ctx = _portfolio_with_strategy(
        "000001.SZ",
        [
            {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
            {"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": 1},
        ],
    )
    # 预置持仓，使 OPEN 和 CLOSE 都可能
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos

    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [
        {"name": "open_sig", "value": 1},
        {"name": "close_sig", "value": 1},
    ]}
    orders = port.on_bar(bar, signal_cache=cache)

    # CLOSE 优先 → 先 SELL 全平；清仓后 OPEN 不再买入（风控优先+清仓后公式不再执行原则）
    assert len(orders) == 1
    assert orders[0].trade_type == TradeType.SELL
    assert orders[0].signal_type == SignalType.CLOSE


def test_on_bar_no_signal_no_order():
    """无信号 → 无订单。"""
    port, ctx = _portfolio_with_strategy(
        "000001.SZ",
        [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}],
    )
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": -1}]}  # 不触发
    orders = port.on_bar(bar, signal_cache=cache)
    assert orders == []


# ===========================================================================
# 下单量参数化（来自策略表，非硬编码 1000 / 0.3）
# 策略资金 = capital_ratio × 组合初始资金；量 = 单只上限/加仓比例 × 策略资金 / 价
# ===========================================================================
def _strategy_with_params(
    stock_code, formula_signals, *, capital_ratio=Decimal("0.6"),
    initial_capital=Decimal("100000"), single_open_ratio=Decimal("0.1"),
    add_position_threshold=Decimal("0.05"), max_add_count=2,
    add_position_ratio=Decimal("0.1"), reduce_position_ratio=Decimal("0.3"),
    max_positions=5, stop_loss=Decimal("0.1"),
):
    """带全量下单参数的策略组合（供下单量测试用）。"""
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=initial_capital, risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=capital_ratio, max_positions=max_positions,
        single_open_ratio=single_open_ratio,
        add_position_threshold=add_position_threshold, max_add_count=max_add_count,
        add_position_ratio=add_position_ratio, reduce_position_ratio=reduce_position_ratio,
    )
    ctx.formula_signals = formula_signals
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=stop_loss, take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)
    return port, ctx


def test_open_signal_quantity_from_single_open_ratio():
    """OPEN 量 = single_open_ratio × 策略资金 / 价，取 100 整数倍。
    资金=0.6×100000=60000，ratio=0.1→6000，价 10.2→588→取 500。"""
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}],
    )
    bar = _bar("000001.SZ", "10.2", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)

    assert len(orders) == 1
    assert orders[0].trade_type == TradeType.BUY
    assert orders[0].quantity == 500


def test_add_signal_respects_threshold_and_count():
    """ADD：现价较成本下跌 ≥ threshold 且 add_count < max_add_count 才出单。
    预置 avg_cost=10，现价 9.4（跌 6% ≥ 5%），add_count=0<2。
    量 = add_position_ratio(0.05)×60000/9.4=319→300。"""
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1}],
        add_position_ratio=Decimal("0.05"),
    )
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos

    bar = _bar("000001.SZ", "9.4", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "add_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)

    assert len(orders) == 1
    assert orders[0].trade_type == TradeType.BUY
    assert orders[0].quantity == 300


def test_add_signal_below_threshold_skipped():
    """现价 9.6（跌 4% < threshold 5%）→ ADD 不出单。"""
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1}],
    )
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos

    bar = _bar("000001.SZ", "9.6", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "add_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    assert orders == []


def test_add_signal_exceeds_max_count_skipped():
    """add_count=2 == max_add_count=2 → ADD 不出单。
    stop_loss 抬高到 0.2，避免现价 9.0（跌 10%）触发止损抢跑。"""
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1}],
        max_add_count=2, stop_loss=Decimal("0.2"),
    )
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    pos.add_count = 2  # 已加满
    ctx.positions["000001.SZ"] = pos

    bar = _bar("000001.SZ", "9.0", datetime(2026, 7, 30, 15, 0))  # 跌 10% ≥ threshold
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "add_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    assert orders == []


def test_reduce_signal_quantity_from_ratio():
    """REDUCE 量 = 持仓 × reduce_position_ratio，取 100 整数倍。
    持仓 1000 × 0.3 = 300。"""
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "reduce_sig", "signal_type": SignalType.REDUCE, "trigger_value": 1}],
    )
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos

    bar = _bar("000001.SZ", "11", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "reduce_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)

    assert len(orders) == 1
    assert orders[0].trade_type == TradeType.SELL
    assert orders[0].quantity == 300


def test_open_signal_respects_max_positions():
    """已有 5 只持仓 = max_positions=5 → 新 OPEN 不出单。"""
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}],
        max_positions=5,
    )
    # 预置 5 只持仓
    for i in range(5):
        code = f"00000{i}.SZ"
        pos = Position(code)
        pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
        ctx.positions[code] = pos
    # 给目标股票也造 bar
    bar = BarEvent(
        stocks={f"00000{i}.SZ": {
            "open": Decimal("10"), "high": Decimal("11"), "low": Decimal("9"),
            "close": Decimal("10.5"), "volume": 1000,
        } for i in range(5)} | {"000001.SZ": {
            "open": Decimal("10"), "high": Decimal("10.6"),
            "low": Decimal("9.9"), "close": Decimal("10.5"), "volume": 1000,
        }},
        bar_time=datetime(2026, 7, 30, 15, 0),
    )
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    assert orders == []


# ===========================================================================
# 主从策略联动（§89）：从策略 OPEN 只能买主策略当前持有的同一只股票；主策略
# 清仓（含该股）后从策略不可新开仓但存量可卖。ADD/REDUCE/全平类不受 master
# 状态约束（仅约束新开仓 OPEN）。
# ===========================================================================
def _master_slave_portfolio(*, slave_role="slave", master_has_position=True,
                            master_holds_stock="000001.SZ"):
    """master(id=1) + slave(id=2, master=1) 双策略组合。
    master_has_position=False 时 master 无持仓，slave OPEN 应被拦。
    slave 目标股 000001.SZ 触 OPEN 信号；master_holds_stock 控制主策略持哪只股。"""
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)

    master = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5, role="master",
    )
    master.formula_signals = []  # master 本测试不触发信号
    master.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.1"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )

    slave = StrategyContext(
        strategy_id=2, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5,
        role=slave_role, master_strategy_id=1 if slave_role == "slave" else None,
    )
    slave.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    slave.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.1"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )

    if master_has_position:
        pos = Position(master_holds_stock)
        pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
        master.positions[master_holds_stock] = pos

    port.strategies.extend([master, slave])
    return port, master, slave


def test_slave_open_blocked_when_master_no_position():
    """master 无持仓 → slave OPEN 信号不出单。"""
    port, master, slave = _master_slave_portfolio(master_has_position=False)
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(2, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    # slave OPEN 被主从守卫拦截
    buy_orders = [o for o in orders if o.trade_type == TradeType.BUY and o.strategy_id == 2]
    assert buy_orders == []


def test_slave_open_allowed_when_master_holds_same_stock():
    """master 持有同一只股 000001.SZ → slave OPEN 该股出单。"""
    port, master, slave = _master_slave_portfolio(master_has_position=True,
                                                  master_holds_stock="000001.SZ")
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(2, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    buy_orders = [o for o in orders if o.trade_type == TradeType.BUY and o.strategy_id == 2]
    assert len(buy_orders) == 1


def test_slave_open_blocked_when_master_holds_other_stock():
    """master 持有 600000.SH（非目标股）→ slave OPEN 000001.SZ 被拦。
    主从联动约束的是"同一只股票"，非"master 有任意持仓"。"""
    port, master, slave = _master_slave_portfolio(master_has_position=True,
                                                  master_holds_stock="600000.SH")
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(2, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    buy_orders = [o for o in orders if o.trade_type == TradeType.BUY and o.strategy_id == 2]
    assert buy_orders == []


def test_slave_sell_allowed_after_master_cleared():
    """master 已清仓，slave 存量持仓 → slave CLOSE SELL 照常出单（不强制平仓）。"""
    port, master, slave = _master_slave_portfolio(master_has_position=False)
    # slave 自身有存量持仓
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    slave.positions["000001.SZ"] = pos
    # slave 配 CLOSE 信号
    slave.formula_signals = [
        {"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": 1},
    ]
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(2, "000001.SZ", bar.bar_time): [{"name": "close_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    sell_orders = [o for o in orders if o.trade_type == TradeType.SELL and o.strategy_id == 2]
    assert len(sell_orders) == 1
    assert sell_orders[0].quantity == 1000


def test_slave_add_allowed_without_master_check():
    """slave 已建仓后 ADD 不受 master 状态约束——master 清仓后 slave ADD 仍可出单。"""
    port, master, slave = _master_slave_portfolio(master_has_position=False)
    # slave 存量持仓（avg_cost=10）
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    slave.positions["000001.SZ"] = pos
    # slave 配 ADD 信号
    slave.formula_signals = [
        {"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1},
    ]
    # 现价 9.4 跌 6% ≥ threshold 5%
    bar = _bar("000001.SZ", "9.4", datetime(2026, 7, 30, 15, 0))
    cache = {(2, "000001.SZ", bar.bar_time): [{"name": "add_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    add_orders = [o for o in orders if o.trade_type == TradeType.BUY
                  and o.strategy_id == 2 and o.signal_type == SignalType.ADD]
    assert len(add_orders) == 1


# ===========================================================================
# 熔断接线（§88）：熔断/日内亏损暂停期间，剥掉新开仓 BUY，保留 SELL（止损/CLOSE）
# ===========================================================================
def test_breaker_suppresses_buy_but_allows_sell():
    """熔断激活时：无持仓股的 OPEN BUY 被剥；有持仓股的止损 SELL 保留。
    用两只股票隔离：A 无持仓（OPEN 信号→应被剥），B 持仓且亏损（止损→应保留）。"""
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [
            {"signal_name": "open_a", "signal_type": SignalType.OPEN, "trigger_value": 1},
            {"signal_name": "close_b", "signal_type": SignalType.CLOSE, "trigger_value": 1},
        ],
        stop_loss=Decimal("0.05"),
    )
    # B 持仓 avg_cost=10
    pos_b = Position("600000.SH")
    pos_b.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["600000.SH"] = pos_b

    # 激活熔断
    port.risk_manager.circuit_breaker_active = True

    # A 无持仓触发 OPEN；B 持仓触发 CLOSE（全平 SELL，不受熔断影响）
    bar = BarEvent(
        stocks={
            "000001.SZ": {"open": Decimal("10"), "high": Decimal("10.6"),
                          "low": Decimal("9.9"), "close": Decimal("10.5"), "volume": 1000},
            "600000.SH": {"open": Decimal("10"), "high": Decimal("10.6"),
                          "low": Decimal("9.9"), "close": Decimal("10.5"), "volume": 1000},
        },
        bar_time=datetime(2026, 7, 30, 15, 0),
    )
    cache = {
        (1, "000001.SZ", bar.bar_time): [{"name": "open_a", "value": 1}],
        (1, "600000.SH", bar.bar_time): [{"name": "close_b", "value": 1}],
    }
    orders = port.on_bar(bar, signal_cache=cache)

    buy_orders = [o for o in orders if o.trade_type == TradeType.BUY]
    sell_orders = [o for o in orders if o.trade_type == TradeType.SELL]
    assert buy_orders == []  # 熔断期间剥 BUY（A 的 OPEN 被剥）
    assert len(sell_orders) == 1  # B 的 CLOSE SELL 保留
    assert sell_orders[0].stock_code == "600000.SH"


def test_breaker_inactive_allows_buy():
    """熔断未激活时 OPEN BUY 正常产出（对照测试）。"""
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}],
    )
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    buy_orders = [o for o in orders if o.trade_type == TradeType.BUY]
    assert len(buy_orders) == 1


def test_check_risks_warns_when_strategy_risk_none(caplog):
    """#29：strategy_risk 未注入 → _check_risks 告警（非静默 return []）+ 不产止损单。

    漏注入时风控静默失效比报错危险（止损/止盈/移动止损全跳过无痕迹）。
    __init__ 声明默认 None + _check_risks None 时 logger.warning 让失效可见。
    """
    import logging
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=42, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5,
    )
    ctx.formula_signals = []  # 无公式信号 → 只有风控可能产单
    # 故意不设 ctx.strategy_risk → 默认 None
    port.strategies.append(ctx)
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos
    # 收盘 9.0 跌 10%，若有 strategy_risk 会触发止损 SELL
    bar = _bar("000001.SZ", "9.0", datetime(2026, 7, 30, 15, 0))

    with caplog.at_level(logging.WARNING, logger="core.engine.portfolio"):
        orders = port.on_bar(bar, signal_cache={})

    # 风控被跳过：无止损 SELL（formula_signals=[] 且风控 None → orders 空）
    assert orders == []
    # 告警可见：含 "has no strategy_risk" + strategy_id 42
    assert any(
        "has no strategy_risk" in r.message and "42" in r.message
        for r in caplog.records
    ), "strategy_risk 未注入应触发 warning，不应静默"


def _buy(price, quantity, trade_time):
    from core.engine.event import TradeEvent
    return TradeEvent(
        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",
        trade_type=TradeType.BUY, price=Decimal(price), quantity=quantity,
        amount=Decimal(price) * quantity, commission=Decimal("0"),
        stamp_duty=Decimal("0"), trade_time=trade_time,
    )
