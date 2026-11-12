import contextlib
import csv
import io
import json
import os
import tempfile
import unittest

from inventory.cli import main


class HiddenTest(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "db.json")
        for args in (["add", "B2", "Tornillo, 5mm", "10", "0.5"], ["add", "A1", "Martillo", "2", "12"],
                     ["add", "C3", 'Llave "inglesa"', "3", "7.25"]):
            main(["--db", self.db] + args, out=io.StringIO())

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main(["--db", self.db] + list(args), out=out)
        return rc, out.getvalue(), err.getvalue()

    def test_export(self):
        rc, out, _ = self.run_cli("export")
        self.assertIn(rc, (0, None))
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual(rows[0], ["sku", "name", "qty", "price", "value"])
        self.assertEqual(rows[1:], [["A1", "Martillo", "2", "12.00", "24.00"],
                                    ["B2", "Tornillo, 5mm", "10", "0.50", "5.00"],
                                    ["C3", 'Llave "inglesa"', "3", "7.25", "21.75"]])
        self.assertNotIn("\r", out)

    def test_export_min_qty(self):
        rc, out, _ = self.run_cli("export", "--min-qty", "3")
        rows = list(csv.reader(io.StringIO(out)))
        self.assertEqual([r[0] for r in rows[1:]], ["B2", "C3"])

    def test_remove_error(self):
        before = open(self.db).read()
        rc, out, err = self.run_cli("remove", "A1", "5")
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error: "), err)
        self.assertEqual(json.loads(open(self.db).read()), json.loads(before))

    def test_value_and_low(self):
        rc, out, _ = self.run_cli("value")
        self.assertEqual(out.strip(), "50.75")
        rc, out, _ = self.run_cli("low", "3")
        self.assertEqual([l.split("\t")[0] for l in out.strip().splitlines()], ["A1", "C3"])


if __name__ == "__main__":
    unittest.main()
