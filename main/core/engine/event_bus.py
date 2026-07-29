from typing import List, Callable, Any, Union
from collections import defaultdict

from tq_iquant_shared.constants import SignalType

from .event import SignalEvent, RiskEvent, OrderEvent


_SIGNAL_PRIORITY = {
    SignalType.CLOSE: 0,
    SignalType.REDUCE: 1,
    SignalType.ADD: 2,
    SignalType.OPEN: 3,
}


class EventBus:
    def __init__(self):
        self._handlers = defaultdict(list)

    def subscribe(self, event_type: type, handler: Callable) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: Any) -> None:
        for handler in self._handlers.get(type(event), []):
            handler(event)

    def process_signals(
        self,
        formula_signals: List[SignalEvent],
        risk_events: List[RiskEvent],
    ) -> List[Union[RiskEvent, SignalEvent]]:
        risk_strategy_ids = {e.strategy_id for e in risk_events if e.strategy_id}

        sorted_risks = sorted(risk_events, key=lambda e: e.strategy_id or 0)

        active_formula = [
            s for s in formula_signals
            if s.strategy_id not in risk_strategy_ids
        ]

        seen: set = set()
        deduped = []
        for s in sorted(active_formula, key=lambda s: (s.strategy_id, _SIGNAL_PRIORITY.get(s.signal_type, 99))):
            key = (s.strategy_id, s.stock_code)
            if key not in seen:
                seen.add(key)
                deduped.append(s)

        return sorted_risks + deduped
