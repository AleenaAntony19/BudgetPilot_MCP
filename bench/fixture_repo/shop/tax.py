"""Sales tax by region."""

RATES = {
    "CA": 0.0725,
    "NY": 0.04,
    "TX": 0.0625,
    "OR": 0.0,
}


def tax_for(region, amount):
    """Tax due on `amount` in `region`. Unknown regions are an error."""
    if region not in RATES:
        raise KeyError(f"no tax rate for region {region}")
    return round(amount * RATES[region], 2)
