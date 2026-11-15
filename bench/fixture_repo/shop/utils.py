"""Small helpers shared by the shop modules."""

import re


def format_money(amount, currency="USD"):
    """Format an amount with two decimals and a currency code, e.g. '12.50 USD'."""
    return f"{amount:.2f} {currency}"


def slugify(text):
    """Lowercase, hyphen-separated identifier: 'Smart Drill!' -> 'smart-drill'."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower())


def clamp(value, low, high):
    return max(low, min(high, value))
