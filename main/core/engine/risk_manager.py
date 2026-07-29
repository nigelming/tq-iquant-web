from decimal import Decimal
from typing import Optional

from .position import Position


class StrategyRiskManager:
    def __init__(
        self,
        stop_loss_ratio: Decimal,
        take_profit_ratio: Decimal,
        trailing_stop_ratio: Decimal,
    ):
        self.stop_loss_ratio = stop_loss_ratio
        self.take_profit_ratio = take_profit_ratio
        self.trailing_stop_ratio = trailing_stop_ratio

    def check_stop_loss(self, position: Position, current_price: Decimal) -> bool:
        if position.quantity == 0:
            return False
        loss = (position.avg_cost - current_price) / position.avg_cost
        return loss >= self.stop_loss_ratio

    def check_take_profit(self, position: Position, current_price: Decimal) -> bool:
        if position.quantity == 0:
            return False
        profit = (current_price - position.avg_cost) / position.avg_cost
        return profit >= self.take_profit_ratio

    def check_trailing_stop(self, position: Position, current_price: Decimal) -> bool:
        if position.quantity == 0 or self.trailing_stop_ratio == 0:
            return False
        if current_price > position.highest_price:
            return False
        drawdown = (position.highest_price - current_price) / position.highest_price
        return drawdown >= self.trailing_stop_ratio


class PortfolioRiskManager:
    def __init__(self, max_drawdown: Decimal, daily_loss_limit: Decimal):
        self.max_drawdown = max_drawdown
        self.daily_loss_limit = daily_loss_limit
        self.consecutive_drawdown_triggers = 0
        self.circuit_breaker_active = False

    def check_max_drawdown(self, current_value: Decimal, peak_value: Decimal) -> bool:
        if peak_value == 0:
            return False
        drawdown = (peak_value - current_value) / peak_value
        return drawdown >= self.max_drawdown

    def check_daily_loss(self, daily_pnl: Decimal, initial_value: Decimal) -> bool:
        if initial_value == 0:
            return False
        loss = abs(daily_pnl) / initial_value if daily_pnl < 0 else 0
        return loss >= self.daily_loss_limit

    def daily_reset(self) -> None:
        self.circuit_breaker_active = False
