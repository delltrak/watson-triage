import unittest

from app.cpf import is_valid


class CpfTests(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(is_valid('529.982.247-25'))

    def test_invalid(self):
        self.assertFalse(is_valid('111.111.111-11'))
        self.assertFalse(is_valid('529.982.247-24'))
