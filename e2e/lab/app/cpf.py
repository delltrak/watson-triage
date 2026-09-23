"""Brazilian CPF check digits."""


def is_valid(cpf):
    digits = [int(c) for c in str(cpf) if c.isdigit()]
    if len(digits) != 11 or len(set(digits)) == 1:
        return False
    for size in (9, 10):
        total = sum(d * w for d, w in zip(digits[:size], range(size + 1, 1, -1)))
        check = (total * 10) % 11 % 10
        if digits[size] != check:
            return False
    return True
