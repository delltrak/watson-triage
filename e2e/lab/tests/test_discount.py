import unittest

from app.discount import apply


class DiscountTests(unittest.TestCase):
    def test_ten_percent(self):
        self.assertEqual(apply(200.0, 10), 180.0)

    def test_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            apply(10.0, 120)
