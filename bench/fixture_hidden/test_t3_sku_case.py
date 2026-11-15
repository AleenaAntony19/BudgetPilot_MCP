from shop import catalog
from shop.errors import UnknownProduct
import pytest


def test_lookup_ignores_case_and_whitespace():
    expected = catalog.find_by_sku("GAR-0001")
    assert catalog.find_by_sku("gar-0001") == expected
    assert catalog.find_by_sku("  Gar-0001 ") == expected
    assert catalog.price_of("off-0003") == 287.26


def test_unknown_still_raises():
    with pytest.raises(UnknownProduct):
        catalog.find_by_sku("zzz-9999")
