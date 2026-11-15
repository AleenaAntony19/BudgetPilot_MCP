from shop.cart import Cart
from shop.errors import InvalidQuantity
import pytest


def test_single_item_subtotal():
    cart = Cart()
    cart.add("GAR-0001")
    assert cart.subtotal() == 91.61


def test_rejects_bad_quantity():
    with pytest.raises(InvalidQuantity):
        Cart().add("GAR-0001", 0)


def test_remove():
    cart = Cart()
    cart.add("GAR-0001")
    cart.remove("GAR-0001")
    assert cart.items == {}
