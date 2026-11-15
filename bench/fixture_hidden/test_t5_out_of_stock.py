from shop.errors import OutOfStock, ShopError
from shop.inventory import Inventory
import pytest


def test_out_of_stock_is_a_shop_error():
    assert issubclass(OutOfStock, ShopError)


def test_reserving_more_than_available_raises_and_reserves_nothing():
    inventory = Inventory()
    available = inventory.available("GAR-0002")          # 27 in stock
    with pytest.raises(OutOfStock):
        inventory.reserve("GAR-0002", available + 1)
    assert inventory.available("GAR-0002") == available


def test_reserving_exactly_the_stock_is_allowed():
    inventory = Inventory()
    assert inventory.reserve("GAR-0002", 27) == 0
    with pytest.raises(OutOfStock):
        inventory.reserve("GAR-0002", 1)
