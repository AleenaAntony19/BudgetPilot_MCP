from shop import report
from shop.cart import Cart


def test_count_is_total_units():
    cart = Cart()
    assert cart.count() == 0
    cart.add("GAR-0001", 2)
    cart.add("GAR-0002")
    assert cart.count() == 3


def test_summary_line_includes_item_count():
    cart = Cart()
    cart.add("GAR-0001")
    assert report.summary_line(cart) == "Cart: 1 item(s), 91.61 USD before tax"
    cart.add("GAR-0001")
    assert report.summary_line(cart).startswith("Cart: 2 item(s), ")
