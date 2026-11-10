class OrderRepository:
    def load(self, order_id: int):
        return {"id": order_id}

    def save(self, order: dict):
        return order
