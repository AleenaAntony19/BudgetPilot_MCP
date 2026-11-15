from shop import pricing
import pytest


def test_percent_discount():
    assert pricing.apply_discount(200.0, 10) == 180.0
    assert pricing.apply_discount(80.0, 25) == 60.0
    assert pricing.apply_discount(19.99, 0) == 19.99
    assert pricing.apply_discount(50.0, 100) == 0.0


def test_invalid_percent_still_rejected():
    with pytest.raises(ValueError):
        pricing.apply_discount(10.0, 101)
