"""Shopping cart totals."""


def total(items):
    """Cart total. Each item is (unit_price, quantity)."""
    return round(sum(price for price, _quantity in items), 2)
