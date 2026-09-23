import unittest

from app.cart import total


class CartTests(unittest.TestCase):
    def test_single_unit(self):
        self.assertEqual(total([(10.0, 1)]), 10.0)

    def test_empty(self):
        self.assertEqual(total([]), 0)
