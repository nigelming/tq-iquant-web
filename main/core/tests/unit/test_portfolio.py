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


def test_on_bar_pool_filter_blocks_cross_pool_order():
    """#3：股票池过滤——池外股票的 OPEN 信号不产生订单（组合2 不会买组合1 池的票）。

    多组合共享行情 bar（bar.stocks 含全局股票）→ 每策略只应交易自己池内股票。
    """
    port, ctx = _portfolio_with_strategy(
        "000001.SZ",
        [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}],
    )
    ctx.stock_pool = {"000001.SZ"}  # 本策略池内只有 000001.SZ
    bar = BarEvent(
        stocks={
            "000001.SZ": {"open": Decimal("10"), "high": Decimal("11"), "low": Decimal("9"), "close": Decimal("10"), "volume": 100},
            "600000.SH": {"open": Decimal("20"), "high": Decimal("21"), "low": Decimal("19"), "close": Decimal("20"), "volume": 200},
        },
        bar_time=datetime(2026, 7, 30, 15, 0),
    )
    # 两只股票 cache 都有 OPEN 信号 → 只应产出池内的 000001.SZ 订单
    cache = {
        (1, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}],
        (1, "600000.SH", bar.bar_time): [{"name": "open_sig", "value": 1}],
    }
    orders = port.on_bar(bar, signal_cache=cache)

    assert len(orders) == 1
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


def test_strategy_fund_capped_by_equity_cap():
    """W3 容量上限：dynamic 资金 = capital_ratio × min(净值, equity_cap)。
    净值超上限 → 资金封顶；未设上限 → 按净值放大（历史行为不变）。"""
    port, ctx = _strategy_with_params("000001.SZ", [])
    # 未设上限：0.6 × 200000 = 120000
    port._bar_equity = Decimal("200000")
    assert port._strategy_fund(ctx) == Decimal("120000")
    # 设上限 50000：0.6 × min(200000, 50000) = 30000（封顶）
    port.equity_cap = Decimal("50000")
    assert port._strategy_fund(ctx) == Decimal("30000")
    # 净值未超上限：0.6 × 40000 = 24000
    port._bar_equity = Decimal("40000")
    assert port._strategy_fund(ctx) == Decimal("24000")
    # fixed 模式不受上限影响：按初始资金
    port.position_sizing = "fixed"
    port.equity_cap = Decimal("50000")
    assert port._strategy_fund(ctx) == ctx.capital_ratio * Decimal("100000")


def test_fee_reserve_setter_syncs_account_cfg():
    """费用预留 setter：置 True 从 cost_params 写入 account.buy_fee_cfg；
    置 False 清回 None（审批恢复仅价×量原行为）。"""
    port = Portfolio(
        portfolio_id=1, initial_capital=Decimal("100000"),
        risk_manager=PortfolioRiskManager(
            max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05")),
        cost_params={"min_commission": Decimal("5"),
                     "buy_commission_rate": Decimal("0.00025")},
    )
    assert port.fee_reserve_enabled is False
    assert port.account.buy_fee_cfg is None
    port.fee_reserve_enabled = True
    assert port.account.buy_fee_cfg == (Decimal("5"), Decimal("0.00025"))
    port.fee_reserve_enabled = False
    assert port.account.buy_fee_cfg is None
    # cost_params 缺费率时不写（保持不启用）
    port2 = Portfolio(
        portfolio_id=2, initial_capital=Decimal("100000"),
        risk_manager=PortfolioRiskManager(
            max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05")),
        cost_params={},
    )
    port2.fee_reserve_enabled = True
    assert port2.account.buy_fee_cfg is None


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


def test_add_signal_threshold_minus_one_adds_on_rise():
    """threshold=-1 特殊值：跳过 drop 检查，上涨也加仓。
    预置 avg_cost=10，现价 11（涨 10%，drop=-0.1），正常阈值会拦。
    stop_loss 抬到 0.2、take_profit 抬到 0.2 避免风控抢跑。
    量 = add_position_ratio(0.05)×策略资金/11：dynamic 资金=0.6×(100000+1000×11)=66600→302.7→300。"""
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1}],
        add_position_threshold=Decimal("-1"),
        add_position_ratio=Decimal("0.05"),
        stop_loss=Decimal("0.2"),
    )
    ctx.strategy_risk.take_profit_ratio = Decimal("0.2")  # 现价涨10% < 20%止盈，不抢跑
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos

    bar = _bar("000001.SZ", "11", datetime(2026, 7, 30, 15, 0))  # 涨 10%（drop=-0.1）
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "add_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)

    assert len(orders) == 1
    assert orders[0].trade_type == TradeType.BUY
    assert orders[0].quantity == 300


def test_add_signal_threshold_minus_one_adds_on_extreme_rise():
    """threshold=-1：涨3倍（drop=-2 < -1）也加——真正"任何价格都加"。
    现有 drop<threshold 逻辑在 drop=-2<-1 时会拦，需显式跳过分支才不拦。
    预置 avg_cost=10，现价 30（涨200%，drop=-2）。止盈抬到 3（300%）避免抢跑。"""
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1}],
        add_position_threshold=Decimal("-1"),
        add_position_ratio=Decimal("0.05"),
        stop_loss=Decimal("0.2"),
    )
    ctx.strategy_risk.take_profit_ratio = Decimal("3")  # 涨200% < 300% 不止盈
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos

    bar = _bar("000001.SZ", "30", datetime(2026, 7, 30, 15, 0))  # 涨200%（drop=-2）
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "add_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)

    assert len(orders) == 1
    assert orders[0].trade_type == TradeType.BUY


def test_add_signal_threshold_minus_one_still_respects_max_count():
    """threshold=-1 时 drop 失效，但 max_add_count 加仓次数上限仍生效。
    add_count=2 == max_add_count=2 → 即便跌够（现价 9.0 跌 10%）也不出单。"""
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "add_sig", "signal_type": SignalType.ADD, "trigger_value": 1}],
        add_position_threshold=Decimal("-1"),
        max_add_count=2, stop_loss=Decimal("0.2"),
    )
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    pos.add_count = 2  # 已加满
    ctx.positions["000001.SZ"] = pos

    bar = _bar("000001.SZ", "9.0", datetime(2026, 7, 30, 15, 0))  # 跌 10%（drop 失效不看）
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


def test_max_positions_block_logs_debug(caplog):
    """① max_positions 拦截 → logger.debug（回测/实盘共用，DEBUG 不刷回测默认 INFO）。

    实盘可调到 DEBUG 级别可见；回测默认 INFO 看不到，不污染输出。
    目标票 000009.SZ 不在预置 5 只持仓内，避免被"本票已持仓"早退拦截。
    """
    import logging
    port, ctx = _strategy_with_params(
        "000009.SZ",
        [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}],
        max_positions=5,
    )
    for i in range(1, 6):
        code = f"00000{i}.SZ"
        pos = Position(code)
        pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
        ctx.positions[code] = pos
    bar = BarEvent(
        stocks={f"00000{i}.SZ": {
            "open": Decimal("10"), "high": Decimal("11"), "low": Decimal("9"),
            "close": Decimal("10.5"), "volume": 1000,
        } for i in range(1, 6)} | {"000009.SZ": {
            "open": Decimal("10"), "high": Decimal("10.6"),
            "low": Decimal("9.9"), "close": Decimal("10.5"), "volume": 1000,
        }},
        bar_time=datetime(2026, 7, 30, 15, 0),
    )
    cache = {(1, "000009.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}]}

    with caplog.at_level(logging.DEBUG, logger="core.engine.portfolio"):
        orders = port.on_bar(bar, signal_cache=cache)

    assert orders == []
    assert any(
        "max_positions" in r.message and r.levelno == logging.DEBUG
        for r in caplog.records
    ), "max_positions 拦截应打 debug 日志"


def test_open_quantity_below_100_block_logs_debug(caplog):
    """② OPEN 算出量 <100 → logger.debug（回测/实盘共用，DEBUG 级别）。"""
    import logging
    # 策略资金 60000 × ratio 0.001 = 60 / 价 10.2 = 5 → <100 拦截
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}],
        single_open_ratio=Decimal("0.001"),
    )
    bar = _bar("000001.SZ", "10.2", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}]}

    with caplog.at_level(logging.DEBUG, logger="core.engine.portfolio"):
        orders = port.on_bar(bar, signal_cache=cache)

    assert orders == []
    assert any(
        "100" in r.message and r.levelno == logging.DEBUG
        for r in caplog.records
    ), "量<100 拦截应打 debug 日志"


def test_reduce_quantity_below_100_block_logs_debug(caplog):
    """③ REDUCE 算出量 <100 → logger.debug（回测/实盘共用，DEBUG 级别）。

    持仓 300 × reduce_ratio 0.3 = 90 <100。现价 11 高于成本 10，不触发止损抢跑。
    """
    import logging
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "reduce_sig", "signal_type": SignalType.REDUCE, "trigger_value": 1}],
        reduce_position_ratio=Decimal("0.3"),
    )
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 300, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos

    bar = _bar("000001.SZ", "11", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "reduce_sig", "value": 1}]}

    with caplog.at_level(logging.DEBUG, logger="core.engine.portfolio"):
        orders = port.on_bar(bar, signal_cache=cache)

    assert orders == []
    assert any(
        "REDUCE" in r.message and r.levelno == logging.DEBUG
        for r in caplog.records
    ), "REDUCE 量<100 拦截应打 debug 日志"


def test_open_signal_ignored_when_stock_already_held():
    """OPEN 信号对本票已持仓时忽略——加仓是 ADD 的职责，OPEN 只开新仓。

    回归：1m 策略公式持续发 OPEN（电平信号），原逻辑只数持仓只数 < max_positions 就放行，
    导致同一只票每分钟加仓一次。本票已持仓时 OPEN 必须直接跳过。
    """
    port, ctx = _strategy_with_params(
        "000001.SZ",
        [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}],
        max_positions=5,
    )
    # 目标股票已持仓（仅 1 只，< max_positions=5，但本票已持有）
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
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


def test_slave_open_blocked_when_master_closes_same_bar():
    """Bug1 回归：master 本 bar 同步清仓 000001.SZ → slave 同 bar OPEN 该股被拦。

    slave 的 OPEN 门禁查 master 的"已成交"持仓（_has_position），此时 master 的
    SELL 尚未成交仍显示持仓 → 旧逻辑误放行，slave 会买入 master 正卖出的同一只
    股票。修复后按本 bar 生成的订单后扫，剥掉同 bar slave 的 OPEN BUY。
    """
    port, master, slave = _master_slave_portfolio(master_has_position=True,
                                                  master_holds_stock="000001.SZ")
    # master 本 bar 触发 CLOSE（清仓 000001.SZ）
    master.formula_signals = [
        {"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": 1},
    ]
    # slave 本 bar 触发 OPEN（同股）
    slave.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {
        (1, "000001.SZ", bar.bar_time): [{"name": "close_sig", "value": 1}],
        (2, "000001.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}],
    }
    orders = port.on_bar(bar, signal_cache=cache)
    master_sells = [o for o in orders if o.trade_type == TradeType.SELL and o.strategy_id == 1]
    assert len(master_sells) == 1
    slave_buys = [o for o in orders if o.trade_type == TradeType.BUY and o.strategy_id == 2]
    assert slave_buys == []


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


def test_halt_strips_buy_logs_debug(caplog):
    """熔断/日内暂停期间剥掉 BUY -> logger.debug（被剥的 BUY 不进返回列表，此处是唯一可见点）。

    DEBUG 级别：熔断期每 bar 每 BUY 都触发，INFO 会刷屏。SELL（止损/平仓）保留（另测）。
    用未持仓的新票出 OPEN BUY，避免被同票 CLOSE 的 cleared 集合抑制。
    """
    import logging
    port, ctx = _portfolio_with_strategy(
        "000009.SZ",
        [{"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1}],
    )
    port.risk_manager.circuit_breaker_active = True  # 熔断中

    bar = _bar("000009.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000009.SZ", bar.bar_time): [{"name": "open_sig", "value": 1}]}

    with caplog.at_level(logging.DEBUG, logger="core.engine.portfolio"):
        orders = port.on_bar(bar, signal_cache=cache)

    assert orders == []  # BUY 被剥
    assert any(
        "剥掉" in r.message and r.levelno == logging.DEBUG
        for r in caplog.records
    ), [r.message for r in caplog.records]


def _buy(price, quantity, trade_time):
    from core.engine.event import TradeEvent
    return TradeEvent(
        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",
        trade_type=TradeType.BUY, price=Decimal(price), quantity=quantity,
        amount=Decimal(price) * quantity, commission=Decimal("0"),
        stamp_duty=Decimal("0"), trade_time=trade_time,
    )


# ===========================================================================
# 组合估值 total_value：残缺 bar / 停牌 必须沿用最近已知价，不能把缺席持仓当 0
# 回归 2026-08-25：BarPoller 逐票完成发残缺 1m bar，旧 total_value 只算
# bar.stocks 内持仓 → 总市值瞬间塌掉 → 误触发 20% max_drawdown 熔断。
# ===========================================================================
def _ohlcv(close):
    return {
        "open": Decimal(str(close)), "high": Decimal(str(close)),
        "low": Decimal(str(close)), "close": Decimal(str(close)), "volume": 1000,
    }


def _portfolio_with_holdings(holdings, cash=Decimal("100000")):
    """holdings: [(code, qty, avg_cost)]。直接构造持仓 + 指定现金，不经撮合。"""
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="1m",
        capital_ratio=Decimal("1"), max_positions=10,
    )
    ctx.formula_signals = []
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.2"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    for code, qty, avg_cost in holdings:
        pos = Position(code)
        pos.quantity = qty
        pos.avg_cost = Decimal(str(avg_cost))
        ctx.positions[code] = pos
    port.strategies.append(ctx)
    port.account.cash = Decimal(str(cash))
    return port


def test_total_value_partial_bar_carries_last_known_price():
    """残缺 1m bar（只含部分持仓）必须用最近已知价补齐缺席持仓，总市值不塌。

    现金 40000 + A 1000 股@10 + B 5000 股@10 = 100000。
    先给全量 bar（A、B 均 10）建立峰值；再给只含 A 的残缺 bar：
    旧逻辑算 40000+10000=50000（B 当 0），新逻辑 B 沿用 10 仍 = 100000。
    """
    port = _portfolio_with_holdings(
        [("000001.SZ", 1000, 10), ("600000.SH", 5000, 10)], cash=40000,
    )
    t1 = datetime(2026, 8, 25, 13, 46)
    full = BarEvent(stocks={"000001.SZ": _ohlcv(10), "600000.SH": _ohlcv(10)}, bar_time=t1)
    assert port.total_value(full) == Decimal("100000")

    t2 = datetime(2026, 8, 25, 13, 47)
    partial = BarEvent(stocks={"000001.SZ": _ohlcv(10)}, bar_time=t2)
    assert port.total_value(partial) == Decimal("100000")  # B 沿用 10，不塌


def test_partial_bar_does_not_false_trigger_max_drawdown():
    """端到端回归：峰值建立后来一根残缺 bar，不应触发 20% max_drawdown。"""
    port = _portfolio_with_holdings(
        [("000001.SZ", 1000, 10), ("600000.SH", 5000, 10)], cash=40000,
    )
    rm = port.risk_manager
    t1 = datetime(2026, 8, 25, 13, 46)
    rm.update_peak(port.total_value(BarEvent(
        stocks={"000001.SZ": _ohlcv(10), "600000.SH": _ohlcv(10)}, bar_time=t1,
    )), t1.date())
    assert rm.peak_value == Decimal("100000")
    assert not rm.circuit_breaker_active

    # 残缺 bar（只有 A）——旧逻辑 50000 对峰值 100000 回撤 50% 会误触发
    t2 = datetime(2026, 8, 25, 13, 47)
    rm.update_peak(port.total_value(BarEvent(
        stocks={"000001.SZ": _ohlcv(10)}, bar_time=t2,
    )), t2.date())
    assert not rm.circuit_breaker_active, "残缺 bar 不应导致 max_drawdown 熔断"
    assert rm.consecutive_drawdown_triggers == 0


def test_total_value_suspended_stock_valued_at_last_close():
    """持仓股全天停牌（后续 bar 完全不含该股）按最近已知收盘价估值。"""
    port = _portfolio_with_holdings(
        [("000001.SZ", 1000, 10), ("600000.SH", 5000, 10)], cash=40000,
    )
    t1 = datetime(2026, 8, 25, 9, 31)
    port.total_value(BarEvent(
        stocks={"000001.SZ": _ohlcv(10), "600000.SH": _ohlcv(10)}, bar_time=t1,
    ))
    # 盘中 B 停牌：后续若干 bar 只有 A，B 永不出现
    for h in (9, 10, 11, 13, 14):
        t = datetime(2026, 8, 25, h, 0)
        val = port.total_value(BarEvent(stocks={"000001.SZ": _ohlcv(10)}, bar_time=t))
        assert val == Decimal("100000"), f"B 停牌应按昨收估值, got {val}"


def test_total_value_zero_close_does_not_poison_snapshot():
    """停牌 bar close=0（TQ NaN 规整而来）不得覆盖快照里的有效价，也不得按 0 估值。

    现金 40000 + A 6000 股@10 = 100000。全量 bar 建立峰值后，A 停牌 close=0：
    若 0 污染快照，A 被估 0 → 总 40000（回撤 60%）误触发；应沿用 10。
    """
    port = _portfolio_with_holdings([("000001.SZ", 6000, 10)], cash=40000)
    rm = port.risk_manager
    t1 = datetime(2026, 8, 25, 9, 31)
    rm.update_peak(port.total_value(BarEvent(
        stocks={"000001.SZ": _ohlcv(10)}, bar_time=t1,
    )), t1.date())

    t2 = datetime(2026, 8, 25, 13, 0)
    suspended = BarEvent(stocks={"000001.SZ": _ohlcv(0)}, bar_time=t2)
    val = port.total_value(suspended)
    assert val == Decimal("100000"), f"close=0 不应被采用, got {val}"
    rm.update_peak(val, t2.date())
    assert not rm.circuit_breaker_active


def test_total_value_uses_avg_cost_before_any_quote():
    """从未在任何 bar 出现过的持仓（有持仓无行情）按 avg_cost 估值，不按 0。"""
    port = _portfolio_with_holdings(
        [("000001.SZ", 1000, 10), ("600000.SH", 5000, 10)], cash=40000,
    )
    # 首根 bar 只有 A，B 从未报过价 → B 按 avg_cost=10 估
    val = port.total_value(BarEvent(
        stocks={"000001.SZ": _ohlcv(10)}, bar_time=datetime(2026, 8, 25, 9, 31),
    ))
    assert val == Decimal("100000")


def test_total_value_snapshot_tracks_real_price_changes():
    """快照必须随有效新价更新，不能冻结在 avg_cost/旧价（保证真实回撤仍能触发）。"""
    port = _portfolio_with_holdings([("000001.SZ", 1000, 10)], cash=90000)
    t1 = datetime(2026, 8, 25, 9, 31)
    assert port.total_value(BarEvent(stocks={"000001.SZ": _ohlcv(10)}, bar_time=t1)) == Decimal("100000")
    # 真实跌到 7：总市值 97000
    t2 = datetime(2026, 8, 25, 10, 0)
    assert port.total_value(BarEvent(stocks={"000001.SZ": _ohlcv(7)}, bar_time=t2)) == Decimal("97000")
    # 再来残缺场景也沿用最新价 7
    t3 = datetime(2026, 8, 25, 10, 1)
    assert port.total_value(BarEvent(stocks={"000001.SZ": _ohlcv(7)}, bar_time=t3)) == Decimal("97000")


# ===========================================================================
# 持仓明细 positions_detail（Task 3 / FR-4）：快照携带 code/quantity/可卖/市值/成本
# 只增字段，供回测落 positions_json；实盘不调用 Portfolio.snapshot，不受影响。
# ===========================================================================
def _portfolio_with_lots(holdings, cash=Decimal("100000")):
    """holdings: [(code, qty, avg_cost, buy_date)]，建立带 T+1 分桶的持仓。"""
    from datetime import date as _date
    port = _portfolio_with_holdings(
        [(c, q, cost) for c, q, cost, _ in holdings], cash=cash,
    )
    ctx = port.strategies[0]
    ctx.positions = {}
    for code, qty, avg_cost, buy_date in holdings:
        pos = Position(code)
        pos.quantity = qty
        pos.avg_cost = Decimal(str(avg_cost))
        pos.buy_time = datetime(buy_date.year, buy_date.month, buy_date.day, 10, 0)
        pos._lots[buy_date] = qty
        ctx.positions[code] = pos
    return port, ctx


def test_snapshot_positions_detail_fields_and_t1():
    """T-1 日买入 1000@10：买入当日可卖 0，次日收盘 10.5 → 可卖 1000、市值 10500。"""
    port, _ = _portfolio_with_lots(
        [("000001.SZ", 1000, 10, datetime(2026, 7, 29).date())], cash=90000,
    )
    # 买入当日（2026-07-29）：T+1 可卖 0，按当日收盘 10 估值
    snap_d0 = port.snapshot(
        datetime(2026, 7, 29).date(), Decimal("100000"),
        _bar("000001.SZ", "10", datetime(2026, 7, 29, 15, 0)),
    )
    p0 = snap_d0["positions"][0]
    assert p0["stock_code"] == "000001.SZ"
    assert p0["quantity"] == 1000
    assert p0["sellable_quantity"] == 0
    assert Decimal(p0["market_value"]) == Decimal("10000")
    assert Decimal(p0["cost_value"]) == Decimal("10000")

    # 次日：可卖 1000，市值按最新收盘 10.5
    snap_d1 = port.snapshot(
        datetime(2026, 7, 30).date(), Decimal("100500"),
        _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0)),
    )
    p1 = snap_d1["positions"][0]
    assert p1["sellable_quantity"] == 1000
    assert Decimal(p1["market_value"]) == Decimal("10500")
    assert Decimal(p1["cost_value"]) == Decimal("10000")


def test_snapshot_positions_empty_list_when_flat():
    """无持仓 → positions 为 []（键存在，空仓日计入 HHI）。"""
    port, _ = _portfolio_with_lots([], cash=100000)
    snap = port.snapshot(
        datetime(2026, 7, 30).date(), Decimal("100000"),
        _bar("000001.SZ", "10", datetime(2026, 7, 30, 15, 0)),
    )
    assert snap["positions"] == []
    # 既有字段不受影响
    assert set(snap) >= {"snap_date", "total_value", "cash", "market_value", "positions"}


def test_positions_detail_aggregates_same_code_across_strategies():
    """组合层：两策略持同一只票 → 聚合成一条，数量/可卖/市值/成本相加，按代码排序。"""
    port, ctx1 = _portfolio_with_lots(
        [("000001.SZ", 1000, 10, datetime(2026, 7, 29).date())], cash=80000,
    )
    ctx2 = StrategyContext(
        strategy_id=2, period="1d",
        capital_ratio=Decimal("0.4"), max_positions=5,
    )
    pos2 = Position("000001.SZ")
    pos2.quantity = 1000
    pos2.avg_cost = Decimal("10")
    pos2.buy_time = datetime(2026, 7, 29, 10, 0)
    pos2._lots[datetime(2026, 7, 29).date()] = 1000
    ctx2.positions["000001.SZ"] = pos2
    port.strategies.append(ctx2)

    port.snapshot(
        datetime(2026, 7, 30).date(), Decimal("100000"),
        _bar("000001.SZ", "10", datetime(2026, 7, 30, 15, 0)),
    )
    detail = port.positions_detail(datetime(2026, 7, 30).date())
    assert len(detail) == 1
    assert detail[0]["quantity"] == 2000
    assert detail[0]["sellable_quantity"] == 2000
    assert Decimal(detail[0]["market_value"]) == Decimal("20000")
    assert Decimal(detail[0]["cost_value"]) == Decimal("20000")


def test_positions_detail_per_strategy_ctx_is_isolated():
    """策略层：ctx 过滤只出该策略持仓。"""
    port, ctx1 = _portfolio_with_lots(
        [("000001.SZ", 1000, 10, datetime(2026, 7, 29).date())], cash=80000,
    )
    ctx2 = StrategyContext(
        strategy_id=2, period="1d",
        capital_ratio=Decimal("0.4"), max_positions=5,
    )
    pos2 = Position("600000.SH")
    pos2.quantity = 500
    pos2.avg_cost = Decimal("20")
    pos2.buy_time = datetime(2026, 7, 29, 10, 0)
    pos2._lots[datetime(2026, 7, 29).date()] = 500
    ctx2.positions["600000.SH"] = pos2
    port.strategies.append(ctx2)

    d1 = port.positions_detail(datetime(2026, 7, 30).date(), ctx=ctx1)
    d2 = port.positions_detail(datetime(2026, 7, 30).date(), ctx=ctx2)
    assert [p["stock_code"] for p in d1] == ["000001.SZ"]
    assert [p["stock_code"] for p in d2] == ["600000.SH"]


# ===========================================================================
# follow_master 状态镜像测试：slave_exit_mode="follow_master" 时，slave 在其自身
# 周期 bar 检测到 master 已不再持有某股 → 全平该股。以 master 的已成交持仓为准，
# 故：master 本 bar 刚发 CLOSE 但未成交时不跟随（下一 slave bar 才跟随）；
# REDUCE 后 master 仍持有 → 不跟随；重启后 master 持仓重建 → 自动补偿。
# ===========================================================================
def _master_slave_with_follow(follow_master=True, slave_holds_stock="000001.SZ",
                              master_holds_stock="000001.SZ"):
    """master(id=1) + slave(id=2, master=1, slave_exit_mode=follow_master) 组合。

    master 默认持 000001.SZ 1000 股、slave 持 500 股。master_holds_stock=None 表示
    master 已清仓（跟随应触发）；slave_holds_stock=None 表示 slave 无持仓（跟随应跳过）。
    """
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)

    master = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5, role="master",
    )
    master.formula_signals = [
        {"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": 1},
    ]
    master.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.1"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    if master_holds_stock:
        mpos = Position(master_holds_stock)
        mpos.apply_trade(_buy("10", 1000, datetime(2026, 7, 29, 9, 30)))
        master.positions[master_holds_stock] = mpos

    slave = StrategyContext(
        strategy_id=2, period="1d",
        capital_ratio=Decimal("0.4"), max_positions=5,
        role="slave", master_strategy_id=1,
        slave_exit_mode="follow_master" if follow_master else "independent",
    )
    slave.formula_signals = []
    slave.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.1"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    if slave_holds_stock:
        spos = Position(slave_holds_stock)
        spos.apply_trade(_buy("10", 500, datetime(2026, 7, 29, 9, 30)))
        slave.positions[slave_holds_stock] = spos

    port.strategies.extend([master, slave])
    return port, master, slave


def test_follow_master_sells_next_slave_bar_after_master_cleared():
    """状态镜像时序：master 本 bar 触发 CLOSE（订单刚生成、未成交）→ slave 不跟随；
    master 清仓成交后（下一 slave bar）→ slave 全平。"""
    port, master, slave = _master_slave_with_follow()
    bar1 = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar1.bar_time): [{"name": "close_sig", "value": 1}]}
    orders1 = port.on_bar(bar1, signal_cache=cache)
    assert [o.signal_name for o in orders1 if o.strategy_id == 1] == ["close_sig"]
    assert [o for o in orders1 if o.signal_name == "follow_master"] == []

    # 模拟 master 卖单在下一 bar 成交 → master 已清仓
    master.positions["000001.SZ"].quantity = 0
    bar2 = _bar("000001.SZ", "10.5", datetime(2026, 7, 31, 15, 0))
    orders2 = port.on_bar(bar2, signal_cache={})
    follow = [o for o in orders2 if o.signal_name == "follow_master"]
    assert len(follow) == 1
    assert follow[0].strategy_id == 2
    assert follow[0].trade_type == TradeType.SELL
    assert follow[0].quantity == 500
    assert follow[0].stock_code == "000001.SZ"


def test_follow_master_triggers_when_master_holds_other_stock():
    """master 持有别的股（非 slave 持仓股）→ 视为未持有该股 → slave 跟随全平。"""
    port, master, slave = _master_slave_with_follow(master_holds_stock="600000.SH")
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    orders = port.on_bar(bar, signal_cache={})

    follow = [o for o in orders if o.signal_name == "follow_master"]
    assert len(follow) == 1
    assert follow[0].quantity == 500


def test_follow_master_skips_if_slave_not_holding():
    """slave 未持仓 → 即使 master 未持有也不出跟随单。"""
    port, master, slave = _master_slave_with_follow(
        slave_holds_stock=None, master_holds_stock=None,
    )
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    orders = port.on_bar(bar, signal_cache={})

    assert [o for o in orders if o.signal_name == "follow_master"] == []


def test_follow_master_dedup_with_own_sell():
    """slave 本 bar 已自行 SELL 该股 → 不再追加 follow_master SELL。"""
    port, master, slave = _master_slave_with_follow(master_holds_stock=None)
    slave.formula_signals = [
        {"signal_name": "close_sig", "signal_type": SignalType.CLOSE, "trigger_value": 1},
    ]
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(2, "000001.SZ", bar.bar_time): [{"name": "close_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)

    assert [o for o in orders if o.signal_name == "follow_master"] == []
    slave_sells = [o for o in orders if o.strategy_id == 2 and o.trade_type == TradeType.SELL]
    assert len(slave_sells) == 1


def test_slave_independent_no_follow_master():
    """independent（默认行为）：master 已清仓 → slave 不跟随，保留持仓。"""
    port, master, slave = _master_slave_with_follow(
        follow_master=False, master_holds_stock=None,
    )
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    orders = port.on_bar(bar, signal_cache={})

    assert [o for o in orders if o.signal_name == "follow_master"] == []


def test_follow_master_reduce_does_not_trigger():
    """master REDUCE（部分减仓，持仓仍 >0）→ slave 不跟随（仅全平类触发）。"""
    port, master, slave = _master_slave_with_follow()  # master 持 1000 股
    master.formula_signals = [
        {"signal_name": "reduce_sig", "signal_type": SignalType.REDUCE, "trigger_value": 1},
    ]
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    cache = {(1, "000001.SZ", bar.bar_time): [{"name": "reduce_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)

    # master 出 REDUCE 卖单；master 仍持仓 → slave 不跟随
    assert [o.signal_name for o in orders if o.strategy_id == 1] == ["reduce_sig"]
    assert [o for o in orders if o.signal_name == "follow_master"] == []


def test_follow_master_multiple_slaves_all_follow():
    """master 清仓 → 其所有 follow_master slave 全部跟随。"""
    port, master, slave = _master_slave_with_follow(master_holds_stock=None)
    slave2 = StrategyContext(
        strategy_id=3, period="1d", capital_ratio=Decimal("0.2"), max_positions=5,
        role="slave", master_strategy_id=1, slave_exit_mode="follow_master",
    )
    slave2.formula_signals = []
    slave2.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.1"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    p2 = Position("000001.SZ")
    p2.apply_trade(_buy("10", 300, datetime(2026, 7, 29, 9, 30)))
    slave2.positions["000001.SZ"] = p2
    port.strategies.append(slave2)

    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 15, 0))
    orders = port.on_bar(bar, signal_cache={})

    follow = {o.strategy_id: o.quantity for o in orders if o.signal_name == "follow_master"}
    assert follow == {2: 500, 3: 300}


def test_follow_master_cross_period_deferred_to_slave_bar():
    """跨周期闭环：master 1d / slave 5m。1d bar 只驱动 master（slave 被 period 过滤）；
    5m bar 驱动 slave 时检出 master 已清仓 → 全平，signal_bar_date=None（非日线）。"""
    port, master, slave = _master_slave_with_follow(master_holds_stock=None)
    slave.period = "5m"

    bar_d = _bar("000001.SZ", "10.5", datetime(2026, 7, 30))
    orders_d = port.on_bar(bar_d, signal_cache={}, period="1d")
    assert [o for o in orders_d if o.signal_name == "follow_master"] == []

    bar5 = _bar("000001.SZ", "10.5", datetime(2026, 7, 30, 9, 35))
    orders5 = port.on_bar(bar5, signal_cache={}, period="5m")
    follow = [o for o in orders5 if o.signal_name == "follow_master"]
    assert len(follow) == 1
    assert follow[0].strategy_id == 2
    assert follow[0].signal_bar_date is None


def test_follow_master_order_carries_signal_bar_date_for_daily_slave():
    """日线 slave 的 follow_master 订单也带 signal_bar_date（延迟到次日成交）。"""
    from datetime import date as _date
    port, master, slave = _master_slave_with_follow(master_holds_stock=None)
    bar = _bar("000001.SZ", "10.5", datetime(2026, 7, 30))
    orders = port.on_bar(bar, signal_cache={})

    follow_sells = [o for o in orders if o.signal_name == "follow_master"]
    assert len(follow_sells) == 1
    assert follow_sells[0].signal_bar_date == _date(2026, 7, 30)


# ===========================================================================
# B4 OPEN 择优轮动（rotation_enabled 默认关）+ B5 状态参数生效
# ===========================================================================
from core.engine.decision import DecisionRecorder


def _buy_code(code, price, quantity, trade_time):
    from core.engine.event import TradeEvent
    return TradeEvent(
        strategy_id=1, portfolio_id=1, stock_code=code,
        trade_type=TradeType.BUY, price=Decimal(str(price)), quantity=quantity,
        amount=Decimal(str(price)) * quantity, commission=Decimal("0"),
        stamp_duty=Decimal("0"), trade_time=trade_time,
    )


def _rotation_portfolio(**kw):
    """max_positions=2、rotation_momentum_bars=3 的轮动组合（默认 rotation_enabled=True）。"""
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.9"), daily_loss_limit=Decimal("0.9"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    params = dict(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("1.0"), max_positions=2,
        single_open_ratio=Decimal("0.1"),
        rotation_enabled=True, rotation_momentum_bars=3,
        rotation_hysteresis=Decimal("0.05"),
    )
    params.update(kw)
    ctx = StrategyContext(**params)
    ctx.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    # 风控全部钝化：止损/止盈极宽、移动止损 0 禁用，不干扰轮动断言
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.5"), take_profit_ratio=Decimal("0.9"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)
    return port, ctx


_ROT_DAYS = [datetime(2026, 7, 27), datetime(2026, 7, 28),
             datetime(2026, 7, 29), datetime(2026, 7, 30)]


def _feed_history(port, closes_by_code, final_signal_code=None):
    """逐日喂 4 根 bar（A/B/C 同 bar）；末日给 final_signal_code 挂 OPEN 信号。"""
    cache = {}
    orders = []
    for i, t in enumerate(_ROT_DAYS):
        stocks = {}
        for code, closes in closes_by_code.items():
            c = Decimal(str(closes[i]))
            stocks[code] = {"open": c, "high": c, "low": c, "close": c, "volume": 1000}
        bar = BarEvent(stocks=stocks, bar_time=t)
        if i == len(_ROT_DAYS) - 1 and final_signal_code:
            cache = {(1, final_signal_code, t): [{"name": "open_sig", "value": 1}]}
        orders = port.on_bar(bar, signal_cache=cache)
        cache = {}
    return orders


def _hold_pair(ctx, a_time=datetime(2026, 7, 1), b_time=datetime(2026, 7, 1)):
    pa = Position("000001.SZ")
    pa.apply_trade(_buy_code("000001.SZ", "7", 1000, a_time))
    ctx.positions["000001.SZ"] = pa
    pb = Position("000002.SZ")
    pb.apply_trade(_buy_code("000002.SZ", "10", 1000, b_time))
    ctx.positions["000002.SZ"] = pb


_STRONG_CLOSES = {
    "000001.SZ": ["10", "9", "8", "7"],      # 动量 -0.30（最弱）
    "000002.SZ": ["10", "10", "10", "10"],   # 动量 0
    "000003.SZ": ["10", "11", "12", "13"],   # 动量 +0.30（新信号）
}


def test_rotation_replaces_weakest_on_strong_signal():
    """满槽 + 强信号（动量 0.30 ≥ 最弱 -0.30 + 滞环 0.05）→ 轮出最弱 A + 换入 C。"""
    port, ctx = _rotation_portfolio()
    rec = DecisionRecorder()
    port.set_recorder(rec)
    _hold_pair(ctx)

    orders = _feed_history(port, _STRONG_CLOSES, final_signal_code="000003.SZ")

    assert len(orders) == 2
    sell, buy = orders
    assert sell.stock_code == "000001.SZ"
    assert sell.trade_type == TradeType.SELL
    assert sell.signal_name == "rotation_exit"
    assert sell.signal_type == SignalType.CLOSE
    assert sell.quantity == 1000
    assert buy.stock_code == "000003.SZ"
    assert buy.trade_type == TradeType.BUY
    assert buy.signal_name == "open_sig"
    assert buy.signal_type == SignalType.OPEN
    assert buy.quantity == 900  # dynamic：int(0.1×117000/13/100)×100（净值=100000+7000+10000）
    # 日线信号两单都带 signal_bar_date（防未来函数闸门同常规）
    assert sell.signal_bar_date == datetime(2026, 7, 30).date()
    assert buy.signal_bar_date == datetime(2026, 7, 30).date()
    assert any(ev.gate == "rotation_replaced" for ev in rec.drain())


def test_rotation_weak_signal_blocked():
    """新信号动量 -0.28 < 最弱 -0.30 + 滞环 0.05 → 不轮动，记 rotation_signal_weak。"""
    port, ctx = _rotation_portfolio()
    rec = DecisionRecorder()
    port.set_recorder(rec)
    _hold_pair(ctx)
    closes = dict(_STRONG_CLOSES)
    closes["000003.SZ"] = ["10", "9", "8.5", "7.2"]  # 动量 -0.28

    orders = _feed_history(port, closes, final_signal_code="000003.SZ")

    assert orders == []
    assert any(ev.gate == "rotation_signal_weak" for ev in rec.drain())


def test_rotation_disabled_keeps_max_positions_full_block():
    """默认（未开 rotation）：满槽 OPEN 一律拦截，记 max_positions_full（原逻辑）。"""
    port, ctx = _rotation_portfolio(rotation_enabled=False)
    rec = DecisionRecorder()
    port.set_recorder(rec)
    _hold_pair(ctx)

    orders = _feed_history(port, _STRONG_CLOSES, final_signal_code="000003.SZ")

    assert orders == []
    gates = {ev.gate for ev in rec.drain()}
    assert "max_positions_full" in gates
    assert "rotation_replaced" not in gates


def test_rotation_skips_same_day_bought_candidate():
    """当日买入的持仓 T+1 不可卖 → 跳过，轮出次弱（B，动量 0）。"""
    port, ctx = _rotation_portfolio()
    _hold_pair(ctx, a_time=datetime(2026, 7, 30, 9, 30))  # A 当日买入不可卖

    orders = _feed_history(port, _STRONG_CLOSES, final_signal_code="000003.SZ")

    assert len(orders) == 2
    assert orders[0].stock_code == "000002.SZ"
    assert orders[0].signal_name == "rotation_exit"


def test_regime_bull_widens_max_positions_in_on_bar():
    """B5：开闸 + bull → 槽位上限取 trend_max_positions(3)，第 3 只 OPEN 放行。"""
    port, ctx = _rotation_portfolio(
        rotation_enabled=False, regime_filter_enabled=True, trend_max_positions=3,
    )
    port.current_regime = "bull"
    _hold_pair(ctx)  # 已持 2 只，原上限 2
    t = datetime(2026, 7, 30)
    bar = BarEvent(stocks={
        "000003.SZ": {"open": Decimal("10"), "high": Decimal("10"),
                      "low": Decimal("10"), "close": Decimal("10"), "volume": 1000},
    }, bar_time=t)
    cache = {(1, "000003.SZ", t): [{"name": "open_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    assert len(orders) == 1
    assert orders[0].trade_type == TradeType.BUY


def test_regime_none_keeps_max_positions_block():
    """B5：开闸但无状态（默认）→ 仍按 max_positions=2 拦截（行为不变）。"""
    port, ctx = _rotation_portfolio(
        rotation_enabled=False, regime_filter_enabled=True, trend_max_positions=3,
    )
    rec = DecisionRecorder()
    port.set_recorder(rec)
    _hold_pair(ctx)
    t = datetime(2026, 7, 30)
    bar = BarEvent(stocks={
        "000003.SZ": {"open": Decimal("10"), "high": Decimal("10"),
                      "low": Decimal("10"), "close": Decimal("10"), "volume": 1000},
    }, bar_time=t)
    cache = {(1, "000003.SZ", t): [{"name": "open_sig", "value": 1}]}
    orders = port.on_bar(bar, signal_cache=cache)
    assert orders == []
    assert any(ev.gate == "max_positions_full" for ev in rec.drain())


def test_regime_bull_relaxes_trailing_stop():
    """B5：开闸 + bull + 覆盖 8% → 回撤 5% 不触发移动止损；无状态（默认）触发 3%。"""
    port, ctx = _rotation_portfolio(
        regime_filter_enabled=True,
        trend_trailing_stop_ratio=Decimal("0.08"),
    )
    # 移动止损实例阈值 3%（覆盖默认 0 禁用）
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.5"), take_profit_ratio=Decimal("0.9"),
        trailing_stop_ratio=Decimal("0.03"),
    )
    pos = Position("000001.SZ")
    pos.apply_trade(_buy_code("000001.SZ", "10", 1000, datetime(2026, 7, 1)))
    pos.update_high(Decimal("10"))
    ctx.positions["000001.SZ"] = pos

    def _bar_95():
        c = Decimal("9.5")  # 较最高 10 回撤 5%
        return BarEvent(stocks={
            "000001.SZ": {"open": c, "high": c, "low": c, "close": c, "volume": 1000},
        }, bar_time=datetime(2026, 7, 30))

    # 默认（无状态）：3% 阈值 → 触发 SELL
    port.current_regime = None
    orders = port.on_bar(_bar_95(), signal_cache={})
    assert len(orders) == 1
    assert orders[0].signal_name == "trailing_stop"
    # 清仓后恢复持仓，切 bull 态：8% 阈值 → 不触发
    ctx.positions["000001.SZ"].quantity = 1000
    port.current_regime = "bull"
    orders = port.on_bar(_bar_95(), signal_cache={})
    assert orders == []
