"""风险/熔断决策事件 message 数值精度（显示层）。"""
import re
from datetime import date, datetime
from decimal import Decimal

from core.engine.account import Account
from core.engine.decision import DecisionRecorder
from core.engine.event import BarEvent, OrderEvent, SignalEvent, TradeEvent
from core.engine.execution_engine import (
    ExecutionEngine, SimulatedDispatcher, SimulatedT1Checker,
)
from core.engine.portfolio import Portfolio
from core.engine.position import Position
from core.engine.risk_manager import PortfolioRiskManager, StrategyRiskManager
from core.engine.strategy_context import StrategyContext
from tq_iquant_shared.constants import SignalType, TradeType


class _ListRecorder:
    def __init__(self):
        self.events = []

    def record(self, **kw):
        self.events.append(kw)


def _mk(rec):
    rm = PortfolioRiskManager(
        max_drawdown=Decimal("0.20"), daily_loss_limit=Decimal("0.05"),
    )
    rm.recorder = rec
    return rm


def test_daily_loss_message_has_no_long_decimal():
    rec = _ListRecorder()
    rm = _mk(rec)
    initial = Decimal("1000000")
    rm.update_peak(Decimal("1369882.7275"), date(2014, 2, 24))        # 首日收盘
    rm.update_peak(Decimal("1369882.7275"), date(2014, 2, 25))        # 跨日：prev_close
    rm.update_peak(Decimal("1304160.2587"), date(2014, 2, 25))        # 当日最后一 bar 收盘
    rm.update_daily(Decimal("1304160.2587"), date(2014, 2, 25), initial)  # pnl≈-65722.47(6.57%) → 暂停
    msgs = [e.get("message", "") for e in rec.events if e.get("gate") == "daily_loss"]
    assert msgs, "应产生 daily_loss 事件"
    joined = " ".join(msgs)
    assert not re.search(r"\.\d{4,}", joined), joined
    assert "6.57%" in joined


def test_max_drawdown_message_has_no_long_decimal():
    rec = _ListRecorder()
    rm = _mk(rec)
    rm.update_peak(Decimal("1369882.7275"), date(2014, 2, 24))
    rm.update_peak(Decimal("1070000.2587"), date(2014, 2, 25))      # 回撤 21.9% → 熔断
    msgs = [e.get("message", "") for e in rec.events if e.get("gate") == "max_drawdown"]
    assert msgs, "应产生 max_drawdown 事件"
    joined = " ".join(msgs)
    assert not re.search(r"\.\d{4,}", joined), joined


# ===========================================================================
# 组合/执行层 message（portfolio.py / execution_engine.py）
# ===========================================================================
_LONG_DEC = re.compile(r"\.\d{4,}")


def _engine(rec):
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    ctx = StrategyContext(
        strategy_id=1, period="1d", capital_ratio=Decimal("0.6"), max_positions=5,
        single_open_ratio=Decimal("0.1"), add_position_threshold=Decimal("0.05"),
        max_add_count=2, add_position_ratio=Decimal("0.1"),
        reduce_position_ratio=Decimal("0.3"),
    )
    ctx.strategy_risk = StrategyRiskManager(
        stop_loss_ratio=Decimal("0.05"), take_profit_ratio=Decimal("0.15"),
        trailing_stop_ratio=Decimal("0.03"),
    )
    port.strategies.append(ctx)
    port.set_recorder(rec)
    return port, ctx


def _hold(ctx, code, price, qty=1000):
    when = datetime(2026, 7, 29, 9, 30)
    pos = Position(code)
    pos.apply_trade(TradeEvent(
        strategy_id=1, portfolio_id=1, stock_code=code, trade_type=TradeType.BUY,
        price=Decimal(str(price)), quantity=qty, amount=Decimal(str(price)) * qty,
        commission=Decimal("0"), stamp_duty=Decimal("0"), trade_time=when,
    ))
    ctx.positions[code] = pos
    return pos


def _ohlcv(close):
    c = Decimal(str(close))
    return {"open": c, "high": c, "low": c, "close": c, "volume": 1000}


def _messages(rec, gate):
    return [e.message for e in rec.drain() if e.gate == gate]


def test_portfolio_stop_loss_message_no_long_decimal():
    rec = DecisionRecorder()
    port, ctx = _engine(rec)
    _hold(ctx, "000001.SZ", "7.42")
    bar = BarEvent(stocks={"000001.SZ": _ohlcv("6.99")}, bar_time=datetime(2026, 7, 30, 15, 0))
    port.on_bar(bar, signal_cache={})
    msgs = _messages(rec, "stop_loss")
    assert msgs and not _LONG_DEC.search(msgs[0]), msgs


def test_portfolio_add_threshold_message_no_long_decimal():
    rec = DecisionRecorder()
    port, ctx = _engine(rec)
    _hold(ctx, "000001.SZ", "7.42")
    bar = BarEvent(stocks={"000001.SZ": _ohlcv("7.30")}, bar_time=datetime(2026, 7, 30, 15, 0))
    sig = SignalEvent(strategy_id=1, stock_code="000001.SZ",
                      signal_name="add_sig", signal_type=SignalType.ADD, bar_time=bar.bar_time)
    assert port._signal_to_order(ctx, sig, bar) == []
    msgs = _messages(rec, "add_threshold_not_met")
    assert msgs and not _LONG_DEC.search(msgs[0]), msgs


def test_execution_engine_capital_messages_no_long_decimal():
    rec = DecisionRecorder()
    eng = ExecutionEngine(SimulatedDispatcher(), SimulatedT1Checker(), recorder=rec)
    order = OrderEvent(
        strategy_id=1, portfolio_id=1, stock_code="000001.SZ", trade_type=TradeType.BUY,
        signal_type=SignalType.OPEN, signal_name="open_sig", quantity=10000,
        price=Decimal("7.4233333"), bar_time=datetime(2026, 7, 30, 15, 0),
    )
    acc = Account(initial_capital=Decimal("100000"))

    acc.cash = Decimal("500.12345")
    assert eng.cap_quantity(order, acc, None) is None
    msgs = _messages(rec, "insufficient_funds")
    assert msgs and not _LONG_DEC.search(msgs[0]), msgs

    acc.cash = Decimal("2500.12345")
    assert eng.cap_quantity(order, acc, None) == 300
    msgs = _messages(rec, "order_shrunk")
    assert msgs and not _LONG_DEC.search(msgs[0]), msgs