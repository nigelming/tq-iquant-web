"""W2.4 Portfolio 现金管理增强预留触发点。

命中阈值仅记一条决策事件（gate=cash_enhance_trigger），不产生任何订单；每交易日去重。
"""
from datetime import datetime
from decimal import Decimal

from core.engine.portfolio import Portfolio
from core.engine.risk_manager import PortfolioRiskManager
from core.engine.event import BarEvent
from core.engine.decision import DecisionRecorder


def _bar(bar_time):
    return BarEvent(
        stocks={"000001.SZ": {
            "open": Decimal("10"), "high": Decimal("10"), "low": Decimal("10"),
            "close": Decimal("10"), "volume": 1000,
        }},
        bar_time=bar_time,
    )


def _mk(**kw):
    pm = PortfolioRiskManager(max_drawdown=Decimal("0.9"), daily_loss_limit=Decimal("0.9"))
    port = Portfolio(portfolio_id=1, initial_capital=Decimal("100000"), risk_manager=pm)
    rec = DecisionRecorder()
    port.set_recorder(rec)
    for k, v in kw.items():
        setattr(port, k, v)
    return port, rec


def test_cash_enhance_triggers_decision_event_no_order():
    port, rec = _mk(cash_enhance_enabled=True,
                    cash_enhance_equity_threshold=Decimal("1000000"))
    port.maybe_cash_enhancement(_bar(datetime(2026, 7, 30, 15, 0)), Decimal("1200000"))
    events = rec.drain()
    assert any(e.gate == "cash_enhance_trigger" for e in events)
    assert all(e.action == "trigger" for e in events if e.gate == "cash_enhance_trigger")


def test_cash_enhance_disabled_no_event():
    port, rec = _mk(cash_enhance_enabled=False,
                    cash_enhance_equity_threshold=Decimal("1"))
    port.maybe_cash_enhancement(_bar(datetime(2026, 7, 30, 15, 0)), Decimal("1200000"))
    assert rec.drain() == []


def test_cash_enhance_below_threshold_no_event():
    port, rec = _mk(cash_enhance_enabled=True,
                    cash_enhance_equity_threshold=Decimal("2000000"))
    port.maybe_cash_enhancement(_bar(datetime(2026, 7, 30, 15, 0)), Decimal("1200000"))
    assert rec.drain() == []


def test_cash_enhance_once_per_day():
    """同一交易日多次调用只记一次。"""
    port, rec = _mk(cash_enhance_enabled=True,
                    cash_enhance_equity_threshold=Decimal("1000000"))
    port.maybe_cash_enhancement(_bar(datetime(2026, 7, 30, 10, 0)), Decimal("1200000"))
    port.maybe_cash_enhancement(_bar(datetime(2026, 7, 30, 15, 0)), Decimal("1200000"))
    events = [e for e in rec.drain() if e.gate == "cash_enhance_trigger"]
    assert len(events) == 1


def test_cash_enhance_ratio_threshold_triggers():
    """持仓占比 ≤ 阈值（现金过多）也命中。"""
    port, rec = _mk(cash_enhance_enabled=True,
                    cash_enhance_position_ratio_threshold=Decimal("0.5"))
    # 无持仓 → holdings_value=0 → ratio=0 ≤ 0.5 → 命中
    port.maybe_cash_enhancement(_bar(datetime(2026, 7, 30, 15, 0)), Decimal("100000"))
    assert any(e.gate == "cash_enhance_trigger" for e in rec.drain())


def test_cash_enhance_none_thresholds_no_event():
    """阈值全 None → 即便开启也不命中。"""
    port, rec = _mk(cash_enhance_enabled=True)
    port.maybe_cash_enhancement(_bar(datetime(2026, 7, 30, 15, 0)), Decimal("1200000"))
    assert rec.drain() == []
