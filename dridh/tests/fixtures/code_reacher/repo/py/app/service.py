from app.repo import OrderRepository


def price_with_tax(amount: float) -> float:
    """Add sales tax to an amount."""
    return amount * 1.2


class OrderService:
    """Order business logic."""

    def __init__(self):
        self.repo = OrderRepository()

    def find(self, order_id: int):
        return self.repo.load(order_id)

    def create(self, payload: dict):
        total = price_with_tax(payload["amount"])
        return self.repo.save({**payload, "total": total})
