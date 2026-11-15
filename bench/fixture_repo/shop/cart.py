"""Shopping cart."""

from shop import catalog, pricing, tax
from shop.errors import InvalidQuantity
from shop.utils import format_money


class Cart:
    def __init__(self, region="OR"):
        self.region = region
        self.items = {}

    def add(self, sku, quantity=1):
        if quantity <= 0:
            raise InvalidQuantity(f"quantity must be positive, got {quantity}")
        catalog.find_by_sku(sku)  # raises UnknownProduct
        self.items[sku] = self.items.get(sku, 0) + quantity

    def remove(self, sku):
        self.items.pop(sku, None)

    def subtotal(self):
        """Sum of unit price times quantity for every line."""
        return pricing.round_price(sum(catalog.price_of(sku) for sku in self.items))

    def total_with_tax(self):
        subtotal = self.subtotal()
        return pricing.round_price(subtotal + tax.tax_for(self.region, subtotal))

    def describe(self):
        return f"{len(self.items)} line(s), {format_money(self.subtotal())}"
