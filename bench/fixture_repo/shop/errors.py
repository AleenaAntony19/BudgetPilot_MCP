"""Exceptions used across the shop package."""


class ShopError(Exception):
    """Base class for shop errors."""


class UnknownProduct(ShopError):
    def __init__(self, sku):
        super().__init__(f"unknown product: {sku}")
        self.sku = sku


class InvalidQuantity(ShopError):
    pass
