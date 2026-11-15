from shop import pricing, tax
from shop.inventory import Inventory
from shop.utils import clamp, format_money


def test_bulk_discount_tiers():
    assert [pricing.bulk_discount_percent(q) for q in (1, 10, 50)] == [0, 5, 15]


def test_tax_known_region():
    assert tax.tax_for("CA", 100.0) == 7.25


def test_format_money():
    assert format_money(3) == "3.00 USD"


def test_clamp():
    assert clamp(15, 0, 10) == 10


def test_inventory_counts_down():
    inventory = Inventory()
    before = inventory.available("GAR-0002")
    inventory.reserve("GAR-0002", 1)
    assert inventory.available("GAR-0002") == before - 1
