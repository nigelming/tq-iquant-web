from datetime import datetime

from core.engine.event import SignalEvent, RiskEvent
from core.engine.event_bus import EventBus
from tq_iquant_shared.constants import SignalType


def test_risk_before_formula():
    bus = EventBus()
    risks = [
        RiskEvent(strategy_id=1, rule="stop_loss"),
    ]
    signals = [
        SignalEvent(strategy_id=1, stock_code="000001.SZ", signal_name="买入",
                     signal_type=SignalType.OPEN, bar_time=datetime.now()),
    ]
    result = bus.process_signals(signals, risks)
    assert len(result) == 1
    assert isinstance(result[0], RiskEvent)


def test_formula_after_risk():
    bus = EventBus()
    signals = [
        SignalEvent(strategy_id=2, stock_code="000001.SZ", signal_name="买入",
                     signal_type=SignalType.OPEN, bar_time=datetime.now()),
        SignalEvent(strategy_id=1, stock_code="000002.SZ", signal_name="卖出",
                     signal_type=SignalType.CLOSE, bar_time=datetime.now()),
    ]
    result = bus.process_signals(signals, [])
    assert len(result) == 2
    assert result[0].strategy_id == 1
    assert result[0].signal_type == SignalType.CLOSE
