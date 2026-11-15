"""Text reports for carts and orders."""

from shop import catalog
from shop.utils import format_money


def summary_line(cart):
    """One line describing a cart, e.g. 'Cart: 12.50 USD before tax'."""
    return f"Cart: {format_money(cart.subtotal())} before tax"


def category_totals(cart):
    """Money spent per category, formatted, sorted by category."""
    totals = {}
    for sku, quantity in cart.items.items():
        _, category, price, _ = catalog.find_by_sku(sku)
        totals[category] = totals.get(category, 0.0) + price * quantity
    return {category: format_money(amount) for category, amount in sorted(totals.items())}
