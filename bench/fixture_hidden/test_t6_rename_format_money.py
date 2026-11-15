import pathlib

import shop.utils
from shop.cart import Cart
from shop.orders import place_order
from shop import report


def test_new_name_exists_and_old_name_is_gone():
    assert shop.utils.format_currency(3) == "3.00 USD"
    assert not hasattr(shop.utils, "format_money")


def test_no_reference_to_the_old_name_remains():
    root = pathlib.Path(shop.utils.__file__).resolve().parent.parent
    offenders = [str(p.relative_to(root)) for p in list(root.glob("shop/*.py")) + list(root.glob("tests/*.py"))
                 if "format_money" in p.read_text()]
    assert offenders == []


def test_callers_still_work():
    cart = Cart()
    cart.add("GAR-0001")
    assert cart.describe() == "1 line(s), 91.61 USD"
    assert report.summary_line(cart).startswith("Cart: 91.61 USD")
    assert "TOTAL 91.61 USD" in place_order(cart).receipt()
