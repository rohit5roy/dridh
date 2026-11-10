from fastapi import FastAPI

from app.service import OrderService, price_with_tax

app = FastAPI()
service = OrderService()


@app.get("/orders/{order_id}")
def read_order(order_id: int):
    """Return one order."""
    return service.find(order_id)


@app.post("/orders")
def create_order(payload: dict):
    """Create an order and return it."""
    return service.create(payload)


def quote(amount: float) -> float:
    return price_with_tax(amount)
