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


def _buy(price, quantity, trade_time):
    from core.engine.event import TradeEvent
    return TradeEvent(
        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",
        trade_type=TradeType.BUY, price=Decimal(price), quantity=quantity,
        amount=Decimal(price) * quantity, commission=Decimal("0"),
        stamp_duty=Decimal("0"), trade_time=trade_time,
    )
