from typing import List, Dict, Optional
from decimal import Decimal
from datetime import date

from .account import Account
from .strategy_context import StrategyContext
from .position import Position
from .risk_manager import PortfolioRiskManager
from .event import BarEvent


class Portfolio:
    def __init__(
        self,
        portfolio_id: int,
        initial_capital: Decimal,
        risk_manager: PortfolioRiskManager,
    ):
        self.portfolio_id = portfolio_id
        self.account = Account(initial_capital)
        self.risk_manager = risk_manager
        self.strategies: List[StrategyContext] = []
        self.benchmark_value: Optional[Decimal] = None

    def on_bar(self, bar: BarEvent) -> None:
        pass

    def check_circuit_breaker(self) -> bool:
        return self.risk_manager.circuit_breaker_active

    def snapshot(self, snap_date: date, current_value: Decimal) -> dict:
        return {
            "snap_date": snap_date,
            "total_value": current_value,
            "cash": self.account.cash,
        }
