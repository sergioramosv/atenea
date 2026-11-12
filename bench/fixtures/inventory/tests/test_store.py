import os
import tempfile
import unittest

from inventory.store import Store


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "db.json")
        self.s = Store(self.db)
        self.s.add("B2", "Tornillo", 10, 0.5)
        self.s.add("A1", "Martillo", 2, 12.0)
        self.s.add("C3", "Llave", 3, 7.25)

    def test_total_value(self):
        self.assertAlmostEqual(self.s.total_value(), 10 * 0.5 + 2 * 12.0 + 3 * 7.25)

    def test_remove_stock(self):
        self.s.remove_stock("B2", 4)
        self.assertEqual(self.s.items["B2"].qty, 6)

    def test_remove_too_much(self):
        with self.assertRaises(ValueError):
            self.s.remove_stock("A1", 3)
        self.assertEqual(self.s.items["A1"].qty, 2)

    def test_remove_non_positive(self):
        with self.assertRaises(ValueError):
            self.s.remove_stock("A1", 0)

    def test_low_stock(self):
        self.assertEqual([i.sku for i in self.s.low_stock(3)], ["A1", "C3"])

    def test_persistence(self):
        self.s.save()
        s2 = Store(self.db)
        self.assertEqual(s2.items["C3"].price, 7.25)


if __name__ == "__main__":
    unittest.main()
