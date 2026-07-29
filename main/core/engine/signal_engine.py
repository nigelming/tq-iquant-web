from typing import List

from .event import SignalEvent, RiskEvent, OrderEvent


class SignalEngine:
    def __init__(self):
        self._signal_handlers = {}

    def register_handler(self, signal_type: str, handler) -> None:
        self._signal_handlers[signal_type] = handler

    def process(
        self,
        signals: List[SignalEvent],
        risks: List[RiskEvent],
    ) -> List[OrderEvent]:
        orders = []
        for sig in signals:
            handler = self._signal_handlers.get(sig.signal_type.value)
            if handler:
                order = handler(sig)
                if order:
                    orders.append(order)
        return orders
