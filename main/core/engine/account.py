from decimal import Decimal
from typing import Optional


class Account:
    def __init__(self, initial_capital: Decimal):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.insufficient_count = 0

    @property
    def market_value(self) -> Decimal:
        return Decimal("0")

    @property
    def total_value(self) -> Decimal:
        return self.cash

    def approve_order(self, quantity: int, price: Decimal, position_value: Decimal) -> tuple:
        needed = price * quantity
        if needed <= self.cash:
            return True, quantity
        max_qty = int(self.cash / price / 100) * 100
        if max_qty >= 100:
            return True, max_qty
        self.insufficient_count += 1
        return False, 0

    def deduct_cash(self, amount: Decimal) -> None:
        self.cash -= amount

    def add_cash(self, amount: Decimal) -> None:
        self.cash += amount
