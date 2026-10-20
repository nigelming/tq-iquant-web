"""W2.2 Portfolio 动态/固定资金口径（_strategy_fund）。

dynamic（默认）：策略资金 = capital_ratio × 组合当前总净值（随盈亏伸缩）；
fixed：策略资金 = capital_ratio × 组合初始资金（旧行为）。
"""
from datetime import datetime
from decimal import Decimal

from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import StrategyRiskManager, PortfolioRiskManager
from core.engine.position import Position
from core.engine.event import BarEvent, TradeEvent
from tq_iquant_shared.constants import TradeType


def _bar(close, bar_time):
    return BarEvent(
        stocks={"000001.SZ": {
            "open": Decimal("10"), "high": Decimal(str(close)),
            "low": Decimal("9"), "close": Decimal(str(close)), "volume": 1000,
        }},
        bar_time=bar_time,
    )


def _buy(price, quantity, trade_time):
    return TradeEvent(
        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",
        trade_type=TradeType.BUY, price=Decimal(price), quantity=quantity,
        amount=Decimal(price) * quantity, commission=Decimal("0"),
        stamp_duty=Decimal("0"), trade_time=trade_time,
    )


def _mk(position_sizing, initial=Decimal("100000")):
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(
        portfolio_id=1, initial_capital=initial, risk_manager=pm,
        position_sizing=position_sizing,
    )
    ctx = StrategyContext(
        strategy_id=1, period="1d", capital_ratio=Decimal("0.6"), max_positions=5,
    )
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.1"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)
    return port, ctx


def test_dynamic_sizing_scales_with_equity():
    """dynamic：净值翻倍后策略资金随之翻倍。"""
    port, ctx = _mk("dynamic")
    # 无持仓：净值 = 现金 = 初始资金
    port.on_bar(_bar("10", datetime(2026, 7, 30, 15, 0)), signal_cache={})
    assert port._strategy_fund(ctx) == ctx.capital_ratio * Decimal("100000")

    # 注入持仓 + 高价 bar → 持仓市值翻倍，组合净值增长 → 策略资金放大
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 10000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos
    port.on_bar(_bar("20", datetime(2026, 7, 31, 15, 0)), signal_cache={})
    equity = port.account.cash + port._holdings_value()
    assert equity > Decimal("100000")
    assert port._strategy_fund(ctx) == ctx.capital_ratio * equity


def test_fixed_sizing_ignores_equity():
    """fixed：净值变化不影响策略资金，恒为 capital_ratio × 初始资金。"""
    port, ctx = _mk("fixed")
    pos = Position("000001.SZ")
    pos.apply_trade(_buy("10", 10000, datetime(2026, 7, 29, 9, 30)))
    ctx.positions["000001.SZ"] = pos
    port.on_bar(_bar("20", datetime(2026, 7, 31, 15, 0)), signal_cache={})
    assert port._strategy_fund(ctx) == ctx.capital_ratio * Decimal("100000")


def test_default_position_sizing_is_dynamic():
    """未显式指定 → 默认 dynamic。"""
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    assert port.position_sizing == "dynamic"
