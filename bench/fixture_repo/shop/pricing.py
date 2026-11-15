"""Discounts and price rounding."""


def apply_discount(total, percent):
    """Reduce `total` by `percent` percent (0-100). 10 percent off 200.0 is 180.0."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return round(total - percent, 2)


def bulk_discount_percent(quantity):
    """Percent off for buying many units of one product."""
    if quantity >= 50:
        return 15
    if quantity >= 10:
        return 5
    return 0


def round_price(value):
    return round(value + 1e-9, 2)
