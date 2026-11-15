"""Turning a cart into an order."""

from shop.utils import format_money

_next_id = 1000


class Order:
    def __init__(self, order_id, lines, total, region):
        self.order_id = order_id
        self.lines = lines
        self.total = total
        self.region = region

    def receipt(self):
        header = f"Order {self.order_id} ({self.region})"
        body = [f"  {sku} x{quantity}" for sku, quantity in sorted(self.lines.items())]
        return "\n".join([header, *body, f"  TOTAL {format_money(self.total)}"])


def place_order(cart):
    global _next_id
    _next_id += 1
    return Order(_next_id, dict(cart.items), cart.total_with_tax(), cart.region)
