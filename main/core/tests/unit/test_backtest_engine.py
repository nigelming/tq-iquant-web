from datetime import datetime, date
from decimal import Decimal

import polars as pl

from core.engine.backtest_engine import BacktestEngine
from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import StrategyRiskManager, PortfolioRiskManager
from core.engine.execution_engine import SimulatedDispatcher
from core.engine.position import Position
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


def _buy_trade(price, quantity, trade_time):
    """构造 BUY TradeEvent 用于预置持仓。"""
    from core.engine.event import TradeEvent
    return TradeEvent(
        strategy_id=1, portfolio_id=1, stock_code="000001.SZ",
        trade_type=TradeType.BUY, price=Decimal(price), quantity=quantity,
        amount=Decimal(price) * quantity, commission=Decimal("0"),
        stamp_duty=Decimal("0"), trade_time=trade_time,
    )


def test_run_breaker_triggers_on_drawdown_and_halts_next_bar_buy():
    """预置持仓后让组合回撤破 20% → 熔断触发；次日 OPEN 信号被剥（无 BUY trade）。

    构造：预置 2000 股 @40（市值 80000）+ 现金 20000 = 总值 100000（峰值）。
    bar1 close=30 → 市值 60000 + 现金 20000 = 80000 → 回撤 20% → update 触发熔断。
    bar1 同时触发 OPEN 信号 → 生成 bar1 BUY 订单（下一 bar open 成交）。
    bar2 open=30 执行 bar1 的 BUY 订单（熔断前已生成，仍成交）。
    bar2 close=30 → update 已熔断；bar2 OPEN 信号 → on_bar 因熔断剥 BUY（无新订单）。
    最终 bar2 无新增 BUY trade（熔断生效）。"""
    stock = "000001.SZ"
    # 4 根 bar，价格从 40 跌到 30 触发回撤
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("40"), Decimal("40"), Decimal("40"), Decimal("40"), 1000),
        (datetime(2026, 7, 30), Decimal("30"), Decimal("30"), Decimal("30"), Decimal("30"), 1000),
        (datetime(2026, 7, 31), Decimal("30"), Decimal("30"), Decimal("30"), Decimal("30"), 1000),
        (datetime(2026, 8, 1), Decimal("30"), Decimal("30"), Decimal("30"), Decimal("30"), 1000),
    ])
    port, ctx = _portfolio_with_strategy(stop_loss=Decimal("0.5"))  # 止损放宽避免抢跑

    # 预置持仓 2000 股 @40，现金调整到 20000
    pos = ctx.positions.setdefault(stock, Position(stock))
    pos.apply_trade(_buy_trade("40", 2000, datetime(2026, 7, 28, 9, 30)))
    port.account.cash = Decimal("20000")  # 总值 80000+20000=100000

    # 每根 bar 都触发 OPEN（试图买入）
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 8, 1)): [{"name": "open_sig", "value": 1}],
    }
    open_prices = {stock: {
        datetime(2026, 7, 30): Decimal("30"),
        datetime(2026, 7, 31): Decimal("30"),
        datetime(2026, 8, 1): Decimal("30"),
    }}

    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache, open_prices=open_prices)

    # bar1(7/29) 收盘价 40 → 峰值 100000，无回撤；OPEN 生成 bar1 订单（bar2 open 成交 #1）。
    # bar2(7/30) 收盘价 30 → 总值 80000，回撤 20% → update 触发熔断；
    #   bar2 on_bar 的 OPEN 订单在熔断前生成（update 在 on_bar 后）→ bar3 open 成交 #2。
    # bar3/bar4 on_bar：熔断已激活 → OPEN BUY 被剥，无新订单 → 无第 3 笔成交。
    assert port.risk_manager.consecutive_drawdown_triggers >= 1
    assert port.risk_manager.circuit_breaker_active is True
    buy_trades = [t for t in result["trades"] if t.trade_type == TradeType.BUY]
    assert len(buy_trades) == 2  # 仅熔断生效前两笔；后续被剥
