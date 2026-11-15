"""Stock reservations on top of the catalogue's stock numbers."""

from shop import catalog


class Inventory:
    def __init__(self):
        self._reserved = {}

    def available(self, sku):
        """Units that can still be reserved."""
        stock = catalog.find_by_sku(sku)[3]
        return stock - self._reserved.get(sku, 0)

    def reserve(self, sku, quantity):
        """Reserve units. Returns the units still available afterwards."""
        self._reserved[sku] = self._reserved.get(sku, 0) + quantity
        return self.available(sku)

    def release(self, sku, quantity):
        self._reserved[sku] = max(0, self._reserved.get(sku, 0) - quantity)
