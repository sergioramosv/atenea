import contextlib
import io
import json
import os
import tempfile
import unittest

from tracker.cli import main
from tracker.query import filter_tasks
from tracker.storage import Storage


class Hidden2(unittest.TestCase):
    def setUp(self):
        self.db = os.path.join(tempfile.mkdtemp(), "db.json")
        for args in (["add", "Diseño", "--priority", "2"],                                  # 1
                     ["add", "API", "--priority", "1", "--tags", "backend", "--after", "1"],  # 2
                     ["add", "UI", "--priority", "2", "--tags", "frontend", "--after", "1"],  # 3
                     ["add", "Docs", "--priority", "3", "--tags", "docs"],                   # 4
                     ["add", "Release", "--priority", "1", "--due", "2026-12-01", "--after", "2,3"]):  # 5
            rc, _, err = self.cli(*args)
            self.assertIn(rc, (0, None), err)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = main(["--db", self.db] + list(args), out=out, today="2026-11-10")
        return rc, out.getvalue(), err.getvalue()

    def raw(self):
        return json.load(open(self.db))

    def ids(self, text):
        return [int(l.split("\t")[0]) for l in text.strip().splitlines() if l.strip()]

    def test_file_v2(self):
        d = self.raw()
        self.assertEqual(d["version"], 2)
        dep = {t["id"]: sorted(t["depends_on"]) for t in d["tasks"]}
        self.assertEqual(dep, {1: [], 2: [1], 3: [1], 4: [], 5: [2, 3]})

    def test_load_v1_and_reject_v3(self):
        p = os.path.join(tempfile.mkdtemp(), "v1.json")
        json.dump({"version": 1, "tasks": [{"id": 7, "title": "x", "status": "open", "priority": 2,
                                            "tags": [], "due": None}]}, open(p, "w"))
        s = Storage(p)
        self.assertEqual(s.tasks[7].depends_on, [])
        json.dump({"version": 3, "tasks": []}, open(p, "w"))
        with self.assertRaises(ValueError):
            Storage(p)

    def test_blocked(self):
        before = self.raw()
        rc, _, err = self.cli("done", "5")
        self.assertEqual(rc, 1)
        self.assertEqual(err.strip(), "error: bloqueada por 2,3")
        rc, _, err = self.cli("start", "2")
        self.assertEqual(rc, 1)
        self.assertEqual(err.strip(), "error: bloqueada por 1")
        self.assertEqual(self.raw(), before)

    def test_next(self):
        rc, out, _ = self.cli("next")
        self.assertEqual(self.ids(out), [1, 4])
        self.cli("done", "1")
        rc, out, _ = self.cli("next")
        self.assertEqual(self.ids(out), [2, 3, 4])
        self.assertEqual(out.splitlines()[0], "2\topen\tP1\tAPI")

    def test_next_order_due(self):
        for args in (["add", "A", "--priority", "3", "--due", "2026-12-20"],   # 6
                     ["add", "B", "--priority", "3", "--due", "2026-11-30"]):  # 7
            self.cli(*args)
        rc, out, _ = self.cli("next")
        self.assertEqual(self.ids(out), [1, 7, 6, 4])

    def test_link_and_cycles(self):
        rc, _, _ = self.cli("link", "4", "--after", "3")
        self.assertIn(rc, (0, None))
        self.assertEqual(sorted(next(t for t in self.raw()["tasks"] if t["id"] == 4)["depends_on"]), [3])
        before = self.raw()
        for bad in (["link", "1", "--after", "5"], ["link", "2", "--after", "2"], ["link", "1", "--after", "99"],
                    ["link", "99", "--after", "1"], ["add", "Z", "--after", "42"], ["done", "77"]):
            rc, _, err = self.cli(*bad)
            self.assertEqual(rc, 1, bad)
            self.assertTrue(err.startswith("error: "), (bad, err))
        self.assertEqual(self.raw(), before)

    def test_query(self):
        s = Storage(self.db)
        T = list(s.tasks.values())
        q = lambda e: sorted(t.id for t in filter_tasks(T, e))
        self.assertEqual(q(""), [1, 2, 3, 4, 5])
        self.assertEqual(q("blocked:yes"), [2, 3, 5])
        self.assertEqual(q("blocked:no"), [1, 4])
        self.assertEqual(q("-tag:docs blocked:no"), [1])
        self.assertEqual(q("tag:backend OR tag:DOCS"), [2, 4])
        self.assertEqual(q("priority<=1 blocked:yes OR status:done"), [2, 5])
        self.assertEqual(q("-priority>=2"), [2, 5])
        for bad in ("OR tag:x", "tag:x OR", "foo:bar", "blocked:maybe"):
            with self.assertRaises(ValueError):
                q(bad)

    def test_cli_invalid_query(self):
        rc, _, err = self.cli("list", "foo:bar")
        self.assertEqual(rc, 1)
        self.assertTrue(err.startswith("error: "))

    def test_report_still_works(self):
        rc, out, _ = self.cli("report")
        self.assertIn("open: 5", out)
        self.assertIn("in_progress: 0", out)
        self.assertIn("overdue: ", out)


if __name__ == "__main__":
    unittest.main()
