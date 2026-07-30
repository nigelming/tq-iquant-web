from datetime import datetime, date
from decimal import Decimal

import polars as pl

from core.engine.backtest_engine import BacktestEngine
from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import StrategyRiskManager, PortfolioRiskManager
from core.engine.execution_engine import SimulatedDispatcher
from tq_iquant_shared.constants import SignalType, TradeType


def _klines(stock_code, rows):
    """构造 Mock klines：单股票单周期日线 polars DataFrame。

    rows: [(date, open, high, low, close, volume), ...]
    """
    df = pl.DataFrame({
        "datetime": [r[0] for r in rows],
        "Open": [r[1] for r in rows],
        "High": [r[2] for r in rows],
        "Low": [r[3] for r in rows],
        "Close": [r[4] for r in rows],
        "Volume": [r[5] for r in rows],
    })
    return {stock_code: {"1d": df}}


def _portfolio_with_strategy(stop_loss=Decimal("0.05")):
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="1d",
        capital_ratio=Decimal("0.6"), max_positions=5,
    )
    ctx.formula_signals = [
        {"signal_name": "open_sig", "signal_type": SignalType.OPEN, "trigger_value": 1},
    ]
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=stop_loss,
        take_profit_ratio=Decimal("0.2"),
        trailing_stop_ratio=Decimal("0"),
    )
    port.strategies.append(ctx)
    return port, ctx


def test_run_minimal_buy_then_stop_loss():
    """3 根日线 bar：bar1 触发 BUY→bar2 open 成交→bar2 close 亏损触发 STOP_LOSS→bar3 open 成交 SELL。
    产出 2 笔 trades + 3 个 snapshots。"""
    stock = "000001.SZ"
    # bar1(7/29): open=10, close=10.2 → on_bar 触发 BUY(OPEN 信号)
    # bar2(7/30): open=10.2 成交 BUY；close=9.0 → 亏损 11.8% > 5% 触发 STOP_LOSS
    # bar3(7/31): open=9.0 成交 SELL（T+1 允许：7/31 > 7/30）
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
        (datetime(2026, 7, 31), Decimal("9.0"), Decimal("9.2"), Decimal("8.8"), Decimal("9.1"), 1000),
    ])
    port, ctx = _portfolio_with_strategy()

    # signal_cache：bar1 触发 OPEN，bar2/bar3 无公式触发
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    }
    # open 价表供 SimulatedDispatcher 成交
    open_prices = {
        stock: {
            datetime(2026, 7, 30): Decimal("10.2"),  # bar2 open 成交 BUY
            datetime(2026, 7, 31): Decimal("9.0"),   # bar3 open 成交 SELL
        }
    }

    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache, open_prices=open_prices)

    trades = result["trades"]
    snapshots = result["snapshots"]
    # 2 笔成交：BUY @10.2，SELL @9.0
    assert len(trades) == 2
    assert trades[0].trade_type == TradeType.BUY
    assert trades[0].price == Decimal("10.2")
    assert trades[1].trade_type == TradeType.SELL
    assert trades[1].price == Decimal("9.0")
    # 3 个日终快照
    assert len(snapshots) == 3
    # 末态：已清仓，cash = 100000 - 买入支出 + 卖出回款
    pos = ctx.positions[stock]
    assert pos.quantity == 0
    # 买入 1000 股 @10.2，金额 10200；卖出 1000 股 @9.0，金额 9000
    # cash = 100000 - (10200 + 买入费用) + (9000 - 卖出费用)
    assert port.account.cash < Decimal("100000")  # 亏损，现金减少


def test_run_no_signal_no_trade():
    """无信号触发 → 0 笔 trades，快照数 = bar 数。"""
    stock = "000001.SZ"
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
    ])
    port, ctx = _portfolio_with_strategy()
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
    }
    open_prices = {stock: {datetime(2026, 7, 30): Decimal("10.2")}}

    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache, open_prices=open_prices)

    assert result["trades"] == []
    assert len(result["snapshots"]) == 2
    # 全程无交易，现金不变
    assert port.account.cash == Decimal("100000")


def test_run_progress_callback():
    """progress_callback 收到每 bar 的进度。"""
    stock = "000001.SZ"
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
    ])
    port, ctx = _portfolio_with_strategy()
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
    }
    progresses = []
    engine = BacktestEngine()
    engine.run(port, klines=klines, signal_cache=cache,
               open_prices={stock: {}}, progress_callback=lambda i: progresses.append(i))

    assert progresses == [1, 2]
