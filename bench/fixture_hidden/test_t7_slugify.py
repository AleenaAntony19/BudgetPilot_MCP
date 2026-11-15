from shop.utils import slugify


def test_no_leading_or_trailing_hyphen():
    assert slugify("Smart Drill!") == "smart-drill"
    assert slugify("  --Eco Headset--  ") == "eco-headset"
    assert slugify("Pro   Drill 2000") == "pro-drill-2000"


def test_empty_and_symbols():
    assert slugify("") == ""
    assert slugify("!!!") == ""
