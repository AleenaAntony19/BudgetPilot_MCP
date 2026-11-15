from shop import catalog
from shop.errors import UnknownProduct
import pytest


def test_lookup_known_sku():
    name, category, price, stock = catalog.find_by_sku("GAR-0001")
    assert (name, category, price, stock) == ("Compact Saw", "office", 91.61, 13)


def test_unknown_sku():
    with pytest.raises(UnknownProduct):
        catalog.find_by_sku("NOPE-0000")


def test_categories_are_sorted():
    assert catalog.categories() == sorted(catalog.categories())
    assert len(catalog.PRODUCTS) == 520
