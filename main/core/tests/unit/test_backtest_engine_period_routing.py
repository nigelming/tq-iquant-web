"""W4.3：BacktestEngine 按周期路由（消混合周期前视）。

背景：旧引擎把所有周期的行合并到同一时间轴，且 on_bar 不传 period，
导致 1d 00:00 日线 bar 驱动 5m 策略、其订单在下一时间轴 bar（当日 09:30）
成交 = 混合周期前视；同 00:00 的 1d/1w/1mon 行还会互相覆盖 OHLC。

新引擎：时间轴为 (datetime, period) 步；价格索引按 (datetime, period) 键；
每步只驱动 period 相同的策略、只成交 order.period 相同的订单。
"""
from datetime import datetime
from decimal import Decimal

import polars as pl

from core.engine.backtest_engine import BacktestEngine
from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import StrategyRiskManager, PortfolioRiskManager
from tq_iquant_shared.constants import SignalType, TradeType


def test_daily_bar_does_not_drive_minute_strategy_same_day():
    """D 00:00 日线 bar 不驱动 5m 策略当日 09:30 成交（消混合周期前视）。

    5m 策略的信号仅存在于日线时刻 D 00:00（模拟按日线时间键控的信号缓存）。
    旧引擎：D 00:00 步 bar 含该票（日线行）→ on_bar 驱动 5m 策略 → BUY 在下一
    时间轴 bar（D 09:30）成交。新引擎：D 00:00 是 1d 步，5m 策略被跳过；
    D 09:30 是 5m 步但该时刻无信号 → 不产生任何成交。
    """
    stock = "000001.SZ"
    D = datetime(2026, 7, 30)
    df_1d = pl.DataFrame({
        "datetime": [D],
        "Open": [Decimal("10")], "High": [Decimal("10.2")],
        "Low": [Decimal("9.9")], "Close": [Decimal("10")], "Volume": [1000],
    })
    df_5m = pl.DataFrame({
        "datetime": [datetime(2026, 7, 30, 9, 30), datetime(2026, 7, 30, 9, 35)],
        "Open": [Decimal("10"), Decimal("10")],
        "High": [Decimal("10.1"), Decimal("10.1")],
        "Low": [Decimal("9.9"), Decimal("9.9")],
        "Close": [Decimal("10"), Decimal("10")],
        "Volume": [500, 500],
    })
    klines = {stock: {"1d": df_1d, "5m": df_5m}}

    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.5"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx_5m = StrategyContext(
        strategy_id=2, period="5m",
        capital_ratio=Decimal("1.0"), max_positions=5,
    )
    ctx_5m.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    ctx_5m.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.05"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx_5m)

    # 5m 策略的信号只在日线时刻 D 00:00 命中（旧引擎会据此在当日 09:30 成交）
    cache = {(2, stock, D): [{"name": "open_sig", "value": 1}]}
    result = BacktestEngine().run(port, klines=klines, signal_cache=cache)

    assert result["trades"] == []


def test_single_period_daily_golden_unchanged():
    """单周期组合行为不变：3 根日线 → 2 笔成交（BUY→STOP_LOSS）+ 3 行快照。"""
    stock = "000001.SZ"
    df_1d = pl.DataFrame({
        "datetime": [
            datetime(2026, 7, 29),
            datetime(2026, 7, 30),
            datetime(2026, 7, 31),
        ],
        "Open": [Decimal("10"), Decimal("10.2"), Decimal("9.0")],
        "High": [Decimal("10.3"), Decimal("10.5"), Decimal("9.2")],
        "Low": [Decimal("9.9"), Decimal("8.9"), Decimal("8.8")],
        "Close": [Decimal("10.2"), Decimal("9.0"), Decimal("9.1")],
        "Volume": [1000, 1000, 1000],
    })
    klines = {stock: {"1d": df_1d}}

    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.5"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5,
    )
    ctx.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.05"), take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)

    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    }
    result = BacktestEngine().run(port, klines=klines, signal_cache=cache)

    trades = result["trades"]
    assert [t.trade_type for t in trades] == [TradeType.BUY, TradeType.SELL]
    assert trades[0].price == Decimal("10.2")
    assert trades[1].price == Decimal("9.0")
    assert trades[1].signal_type == SignalType.STOP_LOSS
    # W4.4：每交易日至一行（3 个交易日 → 3 行）
    assert len(result["snapshots"]) == 3
