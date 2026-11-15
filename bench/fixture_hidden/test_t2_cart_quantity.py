from shop.cart import Cart


def test_subtotal_multiplies_by_quantity():
    cart = Cart()
    cart.add("GAR-0002", 3)
    cart.add("GAR-0001", 2)
    assert cart.subtotal() == round(3 * 14.83 + 2 * 91.61, 2)


def test_single_units_unchanged():
    cart = Cart()
    cart.add("GAR-0001")
    assert cart.subtotal() == 91.61
