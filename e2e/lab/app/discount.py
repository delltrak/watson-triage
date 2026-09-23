"""Price discounts."""


def apply(price, percent):
    """Price after a percentage discount (0-100)."""
    if not 0 <= percent <= 100:
        raise ValueError('percent must be between 0 and 100')
    return round(price * (100 - percent) / 100, 2)
