from datetime import datetime, date
from decimal import Decimal

import polars as pl

from core.engine.backtest_engine import BacktestEngine
from core.engine.portfolio import Portfolio
from core.engine.strategy_context import StrategyContext
from core.engine.risk_manager import StrategyRiskManager, PortfolioRiskManager
from core.engine.evaluator import Evaluator
from tq_iquant_shared.constants import SignalType


def _klines(stock_code, rows):
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


def test_run_produces_evaluations_from_snapshots():
    """BacktestEngine.run 返回 evaluations：从 snapshots 算出的指标。
    BUY@10.2 → STOP_LOSS@9.0 亏损，total_return 应为负。"""
    stock = "000001.SZ"
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
        (datetime(2026, 7, 31), Decimal("9.0"), Decimal("9.2"), Decimal("8.8"), Decimal("9.1"), 1000),
    ])
    port, ctx = _portfolio_with_strategy()
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    }
    open_prices = {stock: {
        datetime(2026, 7, 30): Decimal("10.2"),
        datetime(2026, 7, 31): Decimal("9.0"),
    }}

    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache, open_prices=open_prices)

    evaluations = result["evaluations"]
    assert evaluations is not None
    assert "total_return" in evaluations
    # 亏损交易 → total_return 为负
    assert evaluations["total_return"] < 0


def test_evaluator_accepts_backtest_snapshots():
    """BacktestEngine 产出的 snapshots 直接喂 Evaluator.evaluate 能算出指标。"""
    stock = "000001.SZ"
    klines = _klines(stock, [
        (datetime(2026, 7, 29), Decimal("10"), Decimal("10.3"), Decimal("9.9"), Decimal("10.2"), 1000),
        (datetime(2026, 7, 30), Decimal("10.2"), Decimal("10.5"), Decimal("8.9"), Decimal("9.0"), 1000),
        (datetime(2026, 7, 31), Decimal("9.0"), Decimal("9.2"), Decimal("8.8"), Decimal("9.1"), 1000),
    ])
    port, ctx = _portfolio_with_strategy()
    cache = {
        (1, stock, datetime(2026, 7, 29)): [{"name": "open_sig", "value": 1}],
        (1, stock, datetime(2026, 7, 30)): [{"name": "open_sig", "value": -1}],
        (1, stock, datetime(2026, 7, 31)): [{"name": "open_sig", "value": -1}],
    }
    open_prices = {stock: {
        datetime(2026, 7, 30): Decimal("10.2"),
        datetime(2026, 7, 31): Decimal("9.0"),
    }}

    engine = BacktestEngine()
    result = engine.run(port, klines=klines, signal_cache=cache, open_prices=open_prices)

    ev = Evaluator()
    evaluations = ev.evaluate(result["snapshots"])
    assert evaluations != {}
    assert "total_return" in evaluations
    assert "max_drawdown" in evaluations
