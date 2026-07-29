from decimal import Decimal


class Position:
    def __init__(self, stock_code: str):
        self.stock_code = stock_code
        self.quantity = 0
        self.avg_cost = Decimal("0")
        self.highest_price = Decimal("0")

    @property
    def market_value(self) -> Decimal:
        return self.avg_cost * self.quantity

    def buy(self, quantity: int, price: Decimal) -> None:
        total_cost = self.avg_cost * self.quantity + price * quantity
        self.quantity += quantity
        self.avg_cost = total_cost / self.quantity
        if price > self.highest_price:
            self.highest_price = price

    def sell(self, quantity: int, price: Decimal) -> tuple:
        if quantity > self.quantity:
            quantity = self.quantity
        sell_amount = price * quantity
        cost_amount = self.avg_cost * quantity
        pnl = sell_amount - cost_amount
        self.quantity -= quantity
        return pnl, sell_amount
