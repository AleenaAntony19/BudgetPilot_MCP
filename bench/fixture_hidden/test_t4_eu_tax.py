from shop import tax
from shop.cart import Cart


def test_eu_rate():
    assert tax.tax_for("EU", 100.0) == 20.0


def test_cart_in_eu():
    cart = Cart(region="EU")
    cart.add("GAR-0001")
    assert cart.total_with_tax() == round(91.61 * 1.2, 2)


def test_tax_exempt_cart_pays_no_tax():
    cart = Cart(region="CA", tax_exempt=True)
    cart.add("GAR-0001")
    assert cart.total_with_tax() == 91.61


def test_default_is_not_exempt():
    cart = Cart(region="CA")
    cart.add("GAR-0001")
    assert cart.total_with_tax() == round(91.61 + round(91.61 * 0.0725, 2), 2)
