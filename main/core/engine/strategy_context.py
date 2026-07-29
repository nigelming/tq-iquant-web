from typing import Dict, List
from decimal import Decimal

from .position import Position
from .event import SignalEvent, BarEvent
from tq_iquant_shared.constants import SignalType


class StrategyContext:
    def __init__(
        self,
        strategy_id: int,
        period: str,
        capital_ratio: Decimal,
        max_positions: int,
    ):
        self.strategy_id = strategy_id
        self.period = period
        self.capital_ratio = capital_ratio
        self.max_positions = max_positions
        self.positions: Dict[str, Position] = {}
        self.formula_signals: List[dict] = []

    def get_signal(self, bar: BarEvent) -> List[SignalEvent]:
        return []
